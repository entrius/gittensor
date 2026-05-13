# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Round-level emission allocation by repository ``emission_share``.

Splits the OSS scoring pool (``OSS_EMISSION_SHARE`` of the round) across registered
repos, then within each repo between PR-side and issue-discovery weights using
``issue_discovery_share``, with same-repo spill and recycle for slack.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Set

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation
from gittensor.constants import (
    ISSUES_TREASURY_EMISSION_SHARE,
    ISSUES_TREASURY_UID,
    OSS_EMISSION_SHARE,
    RECYCLE_UID,
)
from gittensor.validator.utils.load_weights import RepositoryConfig

_WEIGHT_EPS = 1e-15


def build_round_reward_vector(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    miner_uids: Set[int],
) -> np.ndarray:
    """Return per-UID reward fractions of the full round (sums to ~1.0).

    - ``OSS_EMISSION_SHARE`` is allocated across repos by ``emission_share``,
      then within each repo to miners proportionally to PR / issue discovery
      scores (with same-repo spill between PR and issue sides).
    - ``(1 - sum(emission_share)) * OSS_EMISSION_SHARE`` plus unclaimed repo
      slices go to ``RECYCLE_UID``.
    - ``ISSUES_TREASURY_EMISSION_SHARE`` is flat to ``ISSUES_TREASURY_UID``.
    """
    sorted_uids = sorted(miner_uids)
    uid_index = {uid: i for i, uid in enumerate(sorted_uids)}
    rewards = np.zeros(len(sorted_uids), dtype=np.float64)

    registry_sum = sum(cfg.emission_share for cfg in master_repositories.values())
    recycle_fraction = OSS_EMISSION_SHARE * max(0.0, 1.0 - registry_sum)

    for repo_key, cfg in master_repositories.items():
        repo_norm = repo_key.lower()
        slice_r = OSS_EMISSION_SHARE * cfg.emission_share
        ids = cfg.issue_discovery_share

        pr_by_uid: Dict[int, float] = defaultdict(float)
        for ev in miner_evaluations.values():
            if ev.failed_reason is not None:
                continue
            for scored in ev.merged_prs:
                if scored.repository_full_name.lower() != repo_norm:
                    continue
                if scored.earned_score > _WEIGHT_EPS:
                    pr_by_uid[ev.uid] += scored.earned_score

        issue_by_uid: Dict[int, float] = defaultdict(float)
        for ev in miner_evaluations.values():
            if ev.failed_reason is not None:
                continue
            w = ev.issue_discovery_repo_scores.get(repo_norm, 0.0)
            if w > _WEIGHT_EPS:
                issue_by_uid[ev.uid] += w

        s_pr = sum(pr_by_uid.values())
        s_is = sum(issue_by_uid.values())

        def _add_pr_pool(pool: float) -> None:
            if pool <= _WEIGHT_EPS or s_pr <= _WEIGHT_EPS:
                return
            for uid, w in pr_by_uid.items():
                idx = uid_index.get(uid)
                if idx is not None:
                    rewards[idx] += pool * (w / s_pr)

        def _add_issue_pool(pool: float) -> None:
            if pool <= _WEIGHT_EPS or s_is <= _WEIGHT_EPS:
                return
            for uid, w in issue_by_uid.items():
                idx = uid_index.get(uid)
                if idx is not None:
                    rewards[idx] += pool * (w / s_is)

        if ids <= _WEIGHT_EPS:
            if s_pr > _WEIGHT_EPS:
                _add_pr_pool(slice_r)
            else:
                recycle_fraction += slice_r
        elif ids >= 1.0 - _WEIGHT_EPS:
            if s_is > _WEIGHT_EPS:
                _add_issue_pool(slice_r)
            else:
                recycle_fraction += slice_r
        else:
            pr_sub = slice_r * (1.0 - ids)
            iss_sub = slice_r * ids
            if s_pr <= _WEIGHT_EPS and s_is <= _WEIGHT_EPS:
                recycle_fraction += slice_r
            elif s_pr <= _WEIGHT_EPS < s_is:
                _add_issue_pool(pr_sub + iss_sub)
            elif s_is <= _WEIGHT_EPS < s_pr:
                _add_pr_pool(pr_sub + iss_sub)
            else:
                _add_pr_pool(pr_sub)
                _add_issue_pool(iss_sub)

    if recycle_fraction > _WEIGHT_EPS and RECYCLE_UID in uid_index:
        rewards[uid_index[RECYCLE_UID]] += recycle_fraction

    if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in uid_index:
        rewards[uid_index[ISSUES_TREASURY_UID]] += ISSUES_TREASURY_EMISSION_SHARE
        bt.logging.info(
            f'Treasury allocation: UID {ISSUES_TREASURY_UID} receives '
            f'{ISSUES_TREASURY_EMISSION_SHARE * 100:.0f}% of emissions'
        )

    total = float(rewards.sum())
    if abs(total - 1.0) > 1e-6:
        bt.logging.warning(f'Round reward vector sums to {total:.9f} (expected 1.0)')

    return rewards
