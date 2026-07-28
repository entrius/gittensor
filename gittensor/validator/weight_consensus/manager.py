# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Snapshot lifecycle for the repo weight consensus.

Aggregates are computed opportunistically right after each snapshot boundary
and persisted to disk as integer numerators, so restarts and an unreachable
contract fall back to the last-good aggregate instead of diverging or
crashing. Transport problems degrade, never raise.
"""

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

import bittensor as bt

from gittensor.validator.utils.config import CONSENSUS_CACHE_KEEP
from gittensor.validator.utils.load_weights import RepositoryConfig
from gittensor.validator.weight_consensus.backend import ConsensusBackend
from gittensor.validator.weight_consensus.consensus import (
    AggregateResult,
    aggregate_preferences,
    apply_consensus,
    compute_snapshot_block,
    shares_from_numerators,
)
from gittensor.validator.weight_consensus.publisher import maybe_publish_prefs, resolve_local_prefs

if TYPE_CHECKING:
    from gittensor.validator.repo_registry.loader import RegistryLoader

StoreHook = Callable[[int, Dict[str, Dict[str, int]], Dict[str, int], Dict[str, bool], AggregateResult], None]


class ConsensusManager:
    """Computes, caches, and serves the per-snapshot aggregate."""

    def __init__(
        self,
        backend: ConsensusBackend,
        cache_dir: Path,
        store_hook: Optional[StoreHook] = None,
        registry_loader: Optional['RegistryLoader'] = None,
    ):
        self.backend = backend
        self.cache_path = Path(cache_dir) / 'weight_consensus_cache.json'
        self.store_hook = store_hook
        self.registry_loader = registry_loader
        self._failed_snapshots: set = set()
        self._cache: Dict[str, Any] = self._load_cache()

    def maybe_refresh(self, block: int) -> None:
        """Cheap per-step tick: compute the current snapshot once, early."""
        snapshot = compute_snapshot_block(block)
        if str(snapshot) in self._cache or snapshot in self._failed_snapshots:
            return
        try:
            self._compute_and_persist(snapshot)
        except Exception as e:
            bt.logging.debug(f'weight_consensus: snapshot {snapshot} refresh failed ({e}); retrying next step')

    def get_shares(self, block: int) -> Optional[Dict[str, float]]:
        """Aggregate shares for the current snapshot, last-good when the
        backend is unreachable, None when nothing is available or the
        activation gate failed."""
        snapshot = compute_snapshot_block(block)
        entry = self._cache.get(str(snapshot))

        if entry is None and snapshot not in self._failed_snapshots:
            try:
                entry = self._compute_and_persist(snapshot)
            except Exception as e:
                self._failed_snapshots.add(snapshot)
                bt.logging.warning(f'weight_consensus: snapshot {snapshot} compute failed ({e})')

        if entry is None:
            previous = [int(b) for b in self._cache if int(b) < snapshot]
            if previous:
                entry = self._cache[str(max(previous))]
                bt.logging.warning(f'weight_consensus: using last-good aggregate from snapshot {max(previous)}')

        if entry is None or not entry['gate_passed']:
            return None
        return shares_from_numerators({repo: int(n) for repo, n in entry['numerators'].items()})

    def _compute_and_persist(self, snapshot: int) -> Dict[str, Any]:
        baskets = self.backend.fetch_baskets(snapshot)
        stakes_rao, permits = self.backend.fetch_stakes_and_permits(snapshot)

        result = aggregate_preferences(baskets, stakes_rao, permits)
        entry = {
            'gate_passed': result.shares is not None,
            'numerators': result.numerators,
            'eligible_stake_rao': result.eligible_stake_rao,
            'valid_stake_rao': result.valid_stake_rao,
            'voter_count': result.voter_count,
        }
        self._cache[str(snapshot)] = entry
        self._save_cache()
        bt.logging.info(
            f'weight_consensus: snapshot {snapshot} aggregated — {result.voter_count} voters, '
            f'gate {"passed" if entry["gate_passed"] else "FAILED (using baked weights)"}'
        )

        if self.store_hook is not None:
            try:
                self.store_hook(snapshot, baskets, stakes_rao, permits, result)
            except Exception as e:
                bt.logging.warning(f'weight_consensus: snapshot store hook failed ({e})')
        return entry

    def _load_cache(self) -> Dict[str, Any]:
        try:
            if self.cache_path.exists():
                return json.loads(self.cache_path.read_text())
        except (OSError, ValueError) as e:
            bt.logging.warning(f'weight_consensus: corrupt cache {self.cache_path} ({e}); starting fresh')
        return {}

    def _save_cache(self) -> None:
        keep = sorted(self._cache, key=int)[-CONSENSUS_CACHE_KEEP:]
        self._cache = {block: self._cache[block] for block in keep}
        try:
            tmp_path = self.cache_path.with_suffix('.tmp')
            tmp_path.write_text(json.dumps(self._cache))
            os.replace(tmp_path, self.cache_path)
        except OSError as e:
            bt.logging.warning(f'weight_consensus: cache write failed ({e})')


def run_weight_consensus(validator, master: Dict[str, RepositoryConfig]) -> Dict[str, RepositoryConfig]:
    """Forward-seam orchestrator: publish own vote, load the on-chain registry
    at the snapshot block, fetch the aggregate, and overlay the shares. The
    contract registry replaces the baked weights only when an aggregate is
    active — contract repos carry no emission_share of their own. Any failure
    returns the baked registry untouched."""
    manager: Optional[ConsensusManager] = getattr(validator, 'consensus_manager', None)
    if getattr(validator.config.neuron, 'disable_weight_consensus', False) or manager is None:
        return master

    try:
        prefs_path = Path(
            validator.config.neuron.consensus_prefs_path
            or Path(validator.config.neuron.full_path) / 'repo_weight_prefs.json'
        )
        prefs = resolve_local_prefs(prefs_path, master)
        if not maybe_publish_prefs(manager.backend, prefs):
            bt.logging.warning(
                'weight_consensus: no preference vector on chain — running as bystander on the '
                'aggregate. Future releases may require participation.'
            )

        shares = manager.get_shares(validator.block)
        if shares is None:
            bt.logging.info('weight_consensus: no active aggregate; using baked-in repository weights')
            return master

        if manager.registry_loader is not None:
            registry = manager.registry_loader.load(compute_snapshot_block(validator.block))
            if registry is not None:
                master = registry
        return apply_consensus(master, shares)
    except Exception as e:
        bt.logging.error(f'weight_consensus: unexpected failure ({e}); using baked-in repository weights')
        return master
