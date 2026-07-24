# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Snapshot lifecycle for the repo weight consensus.

Aggregates are computed opportunistically right after each snapshot boundary
(while lite nodes still serve that state) and persisted to disk as integer
numerators, so restarts and pruned state fall back to the last-good aggregate
instead of diverging or crashing. Chain problems degrade, never raise.
"""

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import bittensor as bt

from gittensor.validator.utils.config import (
    CONSENSUS_CACHE_KEEP,
    CONSENSUS_FRESH_WINDOW_BLOCKS,
)
from gittensor.validator.utils.load_weights import RepositoryConfig
from gittensor.validator.weight_consensus.chain import fetch_all_commitments
from gittensor.validator.weight_consensus.consensus import (
    AggregateResult,
    aggregate_preferences,
    apply_consensus,
    compute_snapshot_block,
    shares_from_numerators,
)
from gittensor.validator.weight_consensus.publisher import ensure_vote

StoreHook = Callable[[int, Dict[str, bytes], Dict[str, int], Dict[str, bool], AggregateResult], None]


class ConsensusManager:
    """Computes, caches, and serves the per-snapshot aggregate."""

    def __init__(self, netuid: int, cache_dir: Path, store_hook: Optional[StoreHook] = None):
        self.netuid = netuid
        self.cache_path = Path(cache_dir) / 'weight_consensus_cache.json'
        self.store_hook = store_hook
        self._failed_snapshots: set = set()
        self._cache: Dict[str, Any] = self._load_cache()

    def maybe_refresh(self, subtensor: 'bt.Subtensor', block: int) -> None:
        """Cheap per-step tick: compute the current snapshot once, while fresh."""
        snapshot = compute_snapshot_block(block)
        if str(snapshot) in self._cache or snapshot in self._failed_snapshots:
            return
        try:
            self._compute_and_persist(subtensor, snapshot)
        except Exception as e:
            if block - snapshot > CONSENSUS_FRESH_WINDOW_BLOCKS:
                self._failed_snapshots.add(snapshot)
                bt.logging.warning(
                    f'weight_consensus: snapshot {snapshot} state unavailable ({e}); '
                    f'using last-good aggregate until the next boundary'
                )
            else:
                bt.logging.debug(f'weight_consensus: snapshot {snapshot} refresh failed ({e}); retrying next step')

    def get_shares(self, subtensor: 'bt.Subtensor', block: int) -> Optional[Dict[str, float]]:
        """Aggregate shares for the current snapshot, last-good on pruned state,
        None when nothing is available or the activation gate failed."""
        snapshot = compute_snapshot_block(block)
        entry = self._cache.get(str(snapshot))

        if entry is None and snapshot not in self._failed_snapshots:
            try:
                entry = self._compute_and_persist(subtensor, snapshot)
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

    def _compute_and_persist(self, subtensor: 'bt.Subtensor', snapshot: int) -> Dict[str, Any]:
        commitments = fetch_all_commitments(subtensor, self.netuid, snapshot)
        metagraph = subtensor.metagraph(self.netuid, block=snapshot, lite=True)
        stakes_rao = {hk: int(round(float(metagraph.S[uid]) * 1e9)) for uid, hk in enumerate(metagraph.hotkeys)}
        permits = {hk: bool(metagraph.validator_permit[uid]) for uid, hk in enumerate(metagraph.hotkeys)}

        result = aggregate_preferences(commitments, stakes_rao, permits)
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
                self.store_hook(snapshot, commitments, stakes_rao, permits, result)
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
    """Forward-seam orchestrator: publish own vote, fetch the aggregate, overlay
    it on the baked registry. Any failure returns the registry untouched."""
    if getattr(validator.config.neuron, 'disable_weight_consensus', False):
        return master

    try:
        prefs_path = Path(
            validator.config.neuron.consensus_prefs_path
            or Path(validator.config.neuron.full_path) / 'repo_weight_prefs.json'
        )
        if not ensure_vote(validator.subtensor, validator.wallet, validator.config.netuid, prefs_path, master):
            bt.logging.warning(
                'weight_consensus: no preference vector on chain — running as bystander on the '
                'aggregate. Future releases may require participation.'
            )

        shares = validator.consensus_manager.get_shares(validator.subtensor, validator.block)
        if shares is None:
            bt.logging.info('weight_consensus: no active aggregate; using baked-in repository weights')
        return apply_consensus(master, shares)
    except Exception as e:
        bt.logging.error(f'weight_consensus: unexpected failure ({e}); using baked-in repository weights')
        return master
