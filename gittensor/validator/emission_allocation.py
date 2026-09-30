# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Round-level emission allocation by repository emission shares."""

from typing import TYPE_CHECKING, Dict, Iterator, Optional

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation, RepoEmissionAllocation
from gittensor.constants import (
    EMISSION_SHARE_TOLERANCE,
    OSS_EMISSION_SHARE,
    RECYCLE_UID,
)
from gittensor.validator.utils.load_weights import RepositoryConfig

if TYPE_CHECKING:
    from gittensor.validator.compute_pool import ComputePool


def blend_emission_pools(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    miner_uids: set[int],
    maintainer_uids_by_repo: Optional[Dict[str, list[int]]] = None,
    compute_pool: Optional['ComputePool'] = None,
) -> np.ndarray:
    """Allocate the combined scoring pool by bounded repository emission_share.

    Each repo's ``emission_share * OSS_EMISSION_SHARE`` slice is distributed
    only within that repo. PR and issue-discovery sub-slices are split by the
    repo's ``issue_discovery_share`` and spill only inside the same repo when
    exactly one side has eligible non-zero scorers. Empty repo slices, the
    decayed-away part of ``absolute_share`` repos, and registry slack recycle to UID 0.

    When a repo sets ``maintainer_cut`` and ``maintainer_uids_by_repo`` lists
    registered maintainer miners for it, ``maintainer_cut`` of that repo's slice
    is carved off the top and split evenly among those maintainers; the
    remainder scores normally. Repos with no listed maintainers are unaffected.

    ``compute_pool`` (with ``COMPUTE_SCORECARD_PATH`` set): the whole share outside ``OSS_EMISSION_SHARE`` is the
    compute pool, paid by the controller scorecard's per-UID shares of it; the unpaid rest (recycle_share,
    unregistered hotkeys, or everything when the scorecard was refused or there is none) recycles.
    """
    sorted_uids = sorted(miner_uids)
    uid_index = {uid: idx for idx, uid in enumerate(sorted_uids)}
    rewards = np.zeros(len(sorted_uids))

    total_configured_share = sum(config.emission_share for config in master_repositories.values())
    recycle_share = max(0.0, 1.0 - total_configured_share) * OSS_EMISSION_SHARE
    # The slice outside the OSS pool burns explicitly: weights are normalized on chain, so leaving it
    # unallocated would silently redistribute it pro-rata instead. With a scorecard that whole slice is the
    # compute pool (below); without one it recycles.
    if compute_pool is None:
        recycle_share += max(0.0, 1.0 - OSS_EMISSION_SHARE)

    for allocation in calculate_repo_emission_breakdown(
        miner_evaluations, master_repositories, miner_uids, maintainer_uids_by_repo
    ):
        recycle_share += allocation.recycled_amount
        for uid, reward in allocation.maintainer_rewards.items():
            rewards[uid_index[uid]] += reward
        for uid, reward in allocation.pr_rewards.items():
            rewards[uid_index[uid]] += reward
        for uid, reward in allocation.issue_discovery_rewards.items():
            rewards[uid_index[uid]] += reward

    if compute_pool is not None:
        # Compute pool: the controller's scorecard weights, each a share of the compute pool; the rest recycles.
        compute_share = max(0.0, 1.0 - OSS_EMISSION_SHARE)
        paid = 0.0
        for uid, share in compute_pool.rewards.items():
            if uid in miner_uids and share > 0:
                rewards[uid_index[uid]] += compute_share * share
                paid += share
        recycle_share += compute_share * max(0.0, 1.0 - paid)
        bt.logging.info(
            f'Compute pool: {paid * 100:.2f}% of the {compute_share * 100:g}% compute share paid to '
            f'{len(compute_pool.rewards)} miner(s)'
            + (f' from scorecard {compute_pool.sha256[:16]}…' if compute_pool.sha256 else f' ({compute_pool.reason})')
        )

    # Recycle receives registry slack and empty repo slices.
    if RECYCLE_UID in miner_uids:
        recycle_idx = uid_index[RECYCLE_UID]
        rewards[recycle_idx] += recycle_share
        if recycle_share > EMISSION_SHARE_TOLERANCE:
            bt.logging.info(f'Recycling {recycle_share * 100:.0f}% unclaimed emissions from repo allocation')

    return rewards


def calculate_repo_emission_breakdown(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    miner_uids: set[int],
    maintainer_uids_by_repo: Optional[Dict[str, list[int]]] = None,
) -> Iterator[RepoEmissionAllocation]:
    """Return per-repository reward allocation details without adding recycle slack.

    Each repo pays only its own ``emission_share * OSS`` slice; nothing pools across repos.
    The maintainer cut comes off the top; the scoring remainder goes to the repo's PR/issue
    scorers, and recycles when the repo has none this round.

    With ``scoring.time_decay.absolute_share`` each sub-slice pays out only
    ``Σ decayed / Σ undecayed`` of itself (pro-rata by decayed score) and recycles the rest,
    so decay shrinks the payout rather than just reweighting scorers.
    """
    maintainer_map = maintainer_uids_by_repo or {}

    for repo_name, repo_config in master_repositories.items():
        if repo_config.emission_share <= 0:
            continue
        allocation = RepoEmissionAllocation(
            repository_full_name=repo_name,
            emission_share=repo_config.emission_share,
            issue_discovery_share=repo_config.issue_discovery_share,
            repo_slice=repo_config.emission_share * OSS_EMISSION_SHARE,
            maintainer_cut=repo_config.maintainer_cut,
        )

        # Maintainer carve-out split evenly among registered maintainers.
        eligible_maintainers = (
            [uid for uid in (maintainer_map.get(repo_name) or []) if uid in miner_uids]
            if repo_config.maintainer_cut > 0.0
            else []
        )
        cut_fraction = repo_config.maintainer_cut if eligible_maintainers else 0.0
        if eligible_maintainers:
            carve_out = repo_config.maintainer_cut * repo_config.emission_share * OSS_EMISSION_SHARE
            per_maintainer = carve_out / len(eligible_maintainers)
            allocation.maintainer_carve_out = carve_out
            allocation.maintainer_rewards = {uid: per_maintainer for uid in eligible_maintainers}

        allocation.pr_scores = _collect_repo_pr_scores(miner_evaluations, repo_name, miner_uids)
        allocation.issue_discovery_scores = _collect_repo_issue_discovery_scores(
            miner_evaluations, repo_name, miner_uids
        )

        scoring_slice = repo_config.emission_share * (1.0 - cut_fraction) * OSS_EMISSION_SHARE
        issue_share = repo_config.issue_discovery_share
        pr_scores = allocation.pr_scores if issue_share < 1.0 else {}
        issue_scores = allocation.issue_discovery_scores if issue_share > 0.0 else {}
        pr_total = sum(pr_scores.values())
        issue_total = sum(issue_scores.values())

        if pr_total > 0 and issue_total > 0:
            allocation.pr_slice = scoring_slice * (1.0 - issue_share)
            allocation.issue_discovery_slice = scoring_slice * issue_share
        elif pr_total > 0:
            allocation.pr_slice = scoring_slice
        elif issue_total > 0:
            allocation.issue_discovery_slice = scoring_slice
        else:
            allocation.recycled_amount += scoring_slice
            yield allocation
            continue

        pr_paid, issue_paid = allocation.pr_slice, allocation.issue_discovery_slice
        if repo_config.scoring.time_decay.absolute_share:
            pr_undecayed, issue_undecayed = _collect_repo_undecayed_totals(miner_evaluations, repo_name, miner_uids)
            pr_paid *= _decayed_fraction(pr_total, pr_undecayed)
            issue_paid *= _decayed_fraction(issue_total, issue_undecayed)

        allocation.pr_rewards, pr_unallocated = _calculate_score_rewards(pr_scores, pr_paid, miner_uids)
        allocation.issue_discovery_rewards, issue_unallocated = _calculate_score_rewards(
            issue_scores, issue_paid, miner_uids
        )
        decayed_away = (allocation.pr_slice - pr_paid) + (allocation.issue_discovery_slice - issue_paid)
        allocation.recycled_amount += decayed_away + pr_unallocated + issue_unallocated
        yield allocation


def _decayed_fraction(decayed_total: float, undecayed_total: float) -> float:
    """Share of a sub-slice an ``absolute_share`` repo pays out: Σ decayed / Σ undecayed, capped at 1."""
    return decayed_total / max(decayed_total, undecayed_total) if decayed_total > 0 else 0.0


def _collect_repo_undecayed_totals(
    miner_evaluations: Dict[int, MinerEvaluation],
    repo_name: str,
    miner_uids: set[int],
) -> tuple[float, float]:
    """Σ undecayed PR and issue-discovery scores across the repo's scoring miners (fully decayed ones included)."""
    pr_total = issue_total = 0.0
    for uid, evaluation in miner_evaluations.items():
        if not _is_scoring_evaluation(uid, evaluation, miner_uids):
            continue
        repo_eval = evaluation.repo_evaluations.get(repo_name)
        if repo_eval is not None:
            pr_total += repo_eval.undecayed_total_score
        issue_total += sum(
            issue.discovery_undecayed_score
            for issue in evaluation.issue_discovery_issues
            if issue.repository_full_name.lower() == repo_name
        )
    return pr_total, issue_total


def _calculate_score_rewards(
    scores: Dict[int, float],
    allocation: float,
    miner_uids: set[int],
) -> tuple[Dict[int, float], float]:
    if allocation <= 0:
        return {}, 0.0

    total = sum(scores.values())
    if total <= 0:
        return {}, allocation

    rewards: Dict[int, float] = {}
    unallocated = 0.0
    for uid, score in scores.items():
        share = allocation * (score / total)
        if uid in miner_uids:
            rewards[uid] = share
        else:
            unallocated += share

    return rewards, unallocated


def _collect_repo_pr_scores(
    miner_evaluations: Dict[int, MinerEvaluation],
    repo_name: str,
    miner_uids: set[int],
) -> Dict[int, float]:
    scores: Dict[int, float] = {}
    for uid, evaluation in miner_evaluations.items():
        if not _is_scoring_evaluation(uid, evaluation, miner_uids):
            continue

        repo_eval = evaluation.repo_evaluations.get(repo_name)
        if repo_eval is None:
            continue

        score = repo_eval.total_score
        if score > 0:
            scores[uid] = score

    return scores


def _collect_repo_issue_discovery_scores(
    miner_evaluations: Dict[int, MinerEvaluation],
    repo_name: str,
    miner_uids: set[int],
) -> Dict[int, float]:
    scores: Dict[int, float] = {}
    for uid, evaluation in miner_evaluations.items():
        if not _is_scoring_evaluation(uid, evaluation, miner_uids):
            continue

        score = sum(
            issue.discovery_earned_score
            for issue in evaluation.issue_discovery_issues
            if issue.repository_full_name.lower() == repo_name and issue.discovery_earned_score > 0
        )
        if score > 0:
            scores[uid] = score

    return scores


def _is_scoring_evaluation(uid: int, evaluation: MinerEvaluation, miner_uids: set[int]) -> bool:
    return uid in miner_uids and evaluation.failed_reason is None
