# The MIT License (MIT)
# Copyright 2025 Entrius
"""On-chain registry -> master repository weights (spec swap R1).

The contract keys repos by GitHub numeric id; this loader resolves them to
lowercase ``owner/name`` dict keys at the load_weights boundary so downstream
scoring sees zero changes (identity shim — renames map forward only). Every
read pins to the snapshot block hash, so all validators load byte-identical
state. Share vectors derive only from contract state at the snapshot block:
the loader never filters or renormalizes on GitHub App install status.

Fallback ladder: contract @ snapshot -> last-good disk cache -> None, which
sends the caller to the baked JSON. A paused or unseeded contract skips the
cache and lands straight on baked (worst case = today's behavior).
"""

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import bittensor as bt

from gittensor.validator.repo_registry.contract_client import RepoRegistryContractClient
from gittensor.validator.utils.load_weights import RepositoryConfig, parse_master_repositories

_FP6 = 1_000_000


def _fp6(value: int) -> float:
    return value / _FP6


def _fp6_or_none(value: int) -> Optional[float]:
    return None if value == 0 else value / _FP6


# Contract param key -> (nested metadata path, field name, decoder).
# Known key -> RepositoryConfig field; missing key -> constants.py default;
# unknown key -> ignored (forward compat).
_PARAM_FIELDS: Dict[int, Tuple[Tuple[str, ...], str, Callable[[int], Any]]] = {
    1: ((), 'issue_discovery_share', _fp6),
    2: ((), 'default_label_multiplier', _fp6),
    3: ((), 'fixed_base_score', _fp6_or_none),  # 0 = unset
    4: ((), 'maintainer_cut', _fp6),
    5: ((), 'trusted_label_pipeline', bool),
    6: (('eligibility',), 'min_valid_merged_prs', int),
    7: (('eligibility',), 'min_credibility', _fp6),
    8: (('eligibility',), 'excessive_pr_penalty_base_threshold', int),
    9: (('eligibility',), 'open_pr_threshold_token_score', _fp6),
    10: (('eligibility',), 'max_open_pr_threshold', int),
    11: (('eligibility',), 'min_valid_solved_issues', int),
    12: (('eligibility',), 'min_issue_credibility', _fp6),
    13: (('eligibility',), 'min_token_score_for_valid_issue', _fp6),
    14: (('eligibility',), 'open_issue_spam_base_threshold', int),
    15: (('eligibility',), 'open_issue_spam_token_score_per_slot', _fp6),
    16: (('eligibility',), 'max_open_issue_threshold', int),
    17: (('scoring',), 'pr_lookback_days', int),
    18: (('scoring',), 'open_pr_collateral_percent', _fp6),
    19: (('scoring',), 'review_penalty_rate', _fp6),
    20: (('scoring',), 'standard_issue_multiplier', _fp6),
    21: (('scoring',), 'maintainer_issue_multiplier', _fp6),
    22: (('scoring',), 'src_tok_saturation_scale', _fp6),
    23: (('scoring', 'time_decay'), 'grace_period_hours', int),
    24: (('scoring', 'time_decay'), 'sigmoid_midpoint_days', _fp6),
    25: (('scoring', 'time_decay'), 'sigmoid_steepness', _fp6),
    26: (('scoring', 'time_decay'), 'min_multiplier', _fp6),
}


class RegistryLoader:
    """Loads the contract registry once per snapshot, pinned to its block hash."""

    def __init__(self, client: RepoRegistryContractClient, cache_dir: Path):
        self.client = client
        self.cache_path = Path(cache_dir) / 'repo_registry_cache.json'
        self._memo: Tuple[Optional[int], Optional[Dict[str, RepositoryConfig]]] = (None, None)

    def load(self, snapshot: int) -> Optional[Dict[str, RepositoryConfig]]:
        """Registry configs at the snapshot block; None -> caller uses baked JSON."""
        memo_snapshot, memo_configs = self._memo
        if memo_snapshot == snapshot:
            return memo_configs
        try:
            at = self.client.subtensor.substrate.get_block_hash(snapshot)
            if not at:
                raise RuntimeError(f'no block hash for snapshot block {snapshot}')
            packed = self.client.get_registry(at)
            if packed is None:
                raise RuntimeError('registry root cell unreadable')
            if packed.paused:
                bt.logging.warning('repo_registry: contract paused; using baked-in repository weights')
                return None
            data = {repo.full_name: self._repo_metadata(repo.github_id, at) for repo in self.client.get_all_repos(at)}
            if not data:
                bt.logging.info('repo_registry: contract registry empty; using baked-in repository weights')
                return None
            configs = parse_master_repositories(data)
            self._save_cache(snapshot, data)
            self._memo = (snapshot, configs)
            bt.logging.info(f'repo_registry: loaded {len(configs)} repos from contract at snapshot {snapshot}')
            return configs
        except Exception as e:
            bt.logging.warning(f'repo_registry: snapshot {snapshot} load failed ({e}); trying last-good cache')
            return self._load_cache()

    def _repo_metadata(self, github_id: int, at: str) -> Dict[str, Any]:
        """Decode one repo's on-chain params into master_repositories.json shape."""
        meta: Dict[str, Any] = {'emission_share': 0.0}  # consensus-voted, overlaid by apply_consensus
        for key, value in sorted(self.client.get_params(github_id, at).items()):
            spec = _PARAM_FIELDS.get(key)
            if spec is None:
                continue
            path, field, decode = spec
            section = meta
            for part in path:
                section = section.setdefault(part, {})
            decoded = decode(value)
            if decoded is not None:
                section[field] = decoded
        labels = self.client.get_label_multipliers(github_id, at)
        if labels:
            meta['label_multipliers'] = {label: _fp6(value) for label, value in sorted(labels.items())}
        patterns = self.client.get_branch_patterns(github_id, at)
        if patterns:
            meta['additional_acceptable_branches'] = patterns
        return meta

    def _save_cache(self, snapshot: int, data: Dict[str, Any]) -> None:
        try:
            tmp_path = self.cache_path.with_suffix('.tmp')
            tmp_path.write_text(json.dumps({'snapshot': snapshot, 'repositories': data}))
            os.replace(tmp_path, self.cache_path)
        except OSError as e:
            bt.logging.warning(f'repo_registry: cache write failed ({e})')

    def _load_cache(self) -> Optional[Dict[str, RepositoryConfig]]:
        try:
            payload = json.loads(self.cache_path.read_text())
            configs = parse_master_repositories(payload['repositories'])
            bt.logging.warning(f'repo_registry: using last-good registry from snapshot {payload["snapshot"]}')
            return configs or None
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as e:
            bt.logging.warning(f'repo_registry: corrupt registry cache ({e}); using baked-in repository weights')
            return None
