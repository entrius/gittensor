# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Per-round emission allocation for the unified OSS + issue-discovery scoring pool.

Replaces the legacy global normalize-then-blend pipeline with allocate-then-distribute:
each registered repo receives ``OSS_EMISSION_SHARE * emission_share`` of the full
round (fractions sum to at most 1.0 across the registry). Within each repo, the
slice splits between PR and issue discovery by ``issue_discovery_share``, with
spill to the active side when the other side has no eligible weight. Unclaimed
repo slices and registry slack ``(1 - Σ emission_share) * OSS_EMISSION_SHARE``
go to ``RECYCLE_UID`` in the same round.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Set, Tuple

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


def _merged_scored_prs(evaluation: MinerEvaluation) -> Tuple:
    return tuple(evaluation.merged_pull_requests) + tuple(evaluation.mirror_merged_prs)


def _repo_pr_weights(
    miner_evaluations: Dict[int, MinerEvaluation],
) -> Dict[str, Tuple[float, Dict[int, float]]]:
    """repo_lower -> (total_weight, uid -> weight) from merged PR earned_score."""
    per_repo_uid: Dict[str, Dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for uid, ev in miner_evaluations.items():
        for pr in _merged_scored_prs(ev):
            repo = pr.repository_full_name.lower()
            per_repo_uid[repo][uid] += max(0.0, float(pr.earned_score))

    out: Dict[str, Tuple[float, Dict[int, float]]] = {}
    for repo, uid_map in per_repo_uid.items():
        total = sum(uid_map.values())
        out[repo] = (total, dict(uid_map))
    return out


def _repo_issue_weights(
    miner_evaluations: Dict[int, MinerEvaluation],
) -> Dict[str, Tuple[float, Dict[int, float]]]:
    """repo_lower -> (total_weight, uid -> weight) from issue discovery (eligible miners only)."""
    per_repo_uid: Dict[str, Dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for uid, ev in miner_evaluations.items():
        if not ev.is_issue_eligible:
            continue
        for repo, score in ev.issue_discovery_repo_scores.items():
            if score > 0:
                per_repo_uid[repo.lower()][uid] += float(score)

    out: Dict[str, Tuple[float, Dict[int, float]]] = {}
    for repo, uid_map in per_repo_uid.items():
        total = sum(uid_map.values())
        out[repo] = (total, dict(uid_map))
    return out


def allocate_round_emissions(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    miner_uids: Set[int],
) -> np.ndarray:
    """Build the final per-UID reward vector (sums to 1.0 when treasury/recycle UIDs exist).

    - ``ISSUES_TREASURY_EMISSION_SHARE`` flat to ``ISSUES_TREASURY_UID`` when present in ``miner_uids``.
    - ``OSS_EMISSION_SHARE`` is split across repos by ``emission_share``, then within each repo by
      PR vs issue discovery with spill; registry slack and inactive repos go to ``RECYCLE_UID``.
    """
    sorted_uids = sorted(miner_uids)
    uid_index = {u: i for i, u in enumerate(sorted_uids)}
    rewards = np.zeros(len(sorted_uids), dtype=np.float64)

    if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in miner_uids:
        rewards[uid_index[ISSUES_TREASURY_UID]] += ISSUES_TREASURY_EMISSION_SHARE
        bt.logging.info(
            f'Treasury allocation: UID {ISSUES_TREASURY_UID} receives '
            f'{ISSUES_TREASURY_EMISSION_SHARE * 100:.0f}% of emissions'
        )

    sum_emission = sum(cfg.emission_share for cfg in master_repositories.values())
    recycle_total = OSS_EMISSION_SHARE * max(0.0, 1.0 - sum_emission)
    if recycle_total > 0 and RECYCLE_UID in miner_uids:
        bt.logging.info(
            f'Registry slack: {(1.0 - sum_emission) * 100:.2f}% of scoring weight → recycle '
            f'({recycle_total * 100:.2f}% of round)'
        )

    pr_by_repo = _repo_pr_weights(miner_evaluations)
    issue_by_repo = _repo_issue_weights(miner_evaluations)

    for repo_name, cfg in master_repositories.items():
        repo_key = repo_name.lower()
        slice_total = OSS_EMISSION_SHARE * float(cfg.emission_share)
        if slice_total <= 0.0:
            continue

        alpha = float(cfg.issue_discovery_share)
        pr_budget_nominal = slice_total * (1.0 - alpha)
        issue_budget_nominal = slice_total * alpha

        wp_total, wp_uid = pr_by_repo.get(repo_key, (0.0, {}))
        wi_total, wi_uid = issue_by_repo.get(repo_key, (0.0, {}))

        if wp_total <= 0.0 and wi_total > 0.0:
            pr_budget, issue_budget = 0.0, slice_total
        elif wi_total <= 0.0 and wp_total > 0.0:
            pr_budget, issue_budget = slice_total, 0.0
        elif wp_total > 0.0 and wi_total > 0.0:
            pr_budget, issue_budget = pr_budget_nominal, issue_budget_nominal
        else:
            recycle_total += slice_total
            continue

        if pr_budget > 0.0 and wp_total > 0.0:
            for uid, w in wp_uid.items():
                if uid in uid_index and w > 0.0:
                    rewards[uid_index[uid]] += pr_budget * (w / wp_total)
        elif pr_budget > 0.0:
            recycle_total += pr_budget

        if issue_budget > 0.0 and wi_total > 0.0:
            for uid, w in wi_uid.items():
                if uid in uid_index and w > 0.0:
                    rewards[uid_index[uid]] += issue_budget * (w / wi_total)
        elif issue_budget > 0.0:
            recycle_total += issue_budget

    if recycle_total > 0.0 and RECYCLE_UID in miner_uids:
        rewards[uid_index[RECYCLE_UID]] += recycle_total

    total = float(rewards.sum())
    if abs(total - 1.0) > 1e-5:
        bt.logging.warning(f'Emission allocation sum is {total:.6f} (expected ~1.0)')

    return rewards
