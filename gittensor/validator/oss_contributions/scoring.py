# The MIT License (MIT)
# Copyright © 2025 Entrius

from typing import TYPE_CHECKING, Dict, Optional

import bittensor as bt

from gittensor.classes import MinerEvaluation

if TYPE_CHECKING:
    from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR
from gittensor.constants import (
    EXCESSIVE_PR_PENALTY_BASE_THRESHOLD,
    MAX_OPEN_PR_REVIEW_COLLATERAL_MULTIPLIER,
    MAX_OPEN_PR_THRESHOLD,
    OPEN_PR_COLLATERAL_PERCENT,
    OPEN_PR_THRESHOLD_TOKEN_SCORE,
    REVIEW_PENALTY_RATE,
)
from gittensor.validator.oss_contributions.credibility import check_eligibility
from gittensor.validator.utils.load_weights import RepositoryConfig


def calculate_review_quality_multiplier(changes_requested_count: int, pr_number: Optional[int] = None) -> float:
    """Calculate the review quality multiplier based on maintainer CHANGES_REQUESTED reviews.

    Formula: max(0.0, 1.0 - REVIEW_PENALTY_RATE × N)
    """
    multiplier = max(0.0, 1.0 - REVIEW_PENALTY_RATE * changes_requested_count)
    if changes_requested_count > 0:
        ctx = f' (PR #{pr_number})' if pr_number else ''
        bt.logging.info(
            f'{changes_requested_count} maintainer CHANGES_REQUESTED review(s){ctx} → '
            f'review_quality_multiplier={multiplier:.2f}'
        )
    return multiplier


def calculate_review_collateral_multiplier(changes_requested_count: int, pr_number: Optional[int] = None) -> float:
    """Calculate the open-PR collateral multiplier from maintainer CHANGES_REQUESTED reviews.

    Unlike ``review_quality_multiplier`` for earned scores, this increases
    collateral so non-merge-ready open PRs reserve more score instead of less.
    Formula: min(MAX_OPEN_PR_REVIEW_COLLATERAL_MULTIPLIER, 1.0 + REVIEW_PENALTY_RATE × N)
    """
    multiplier = min(
        MAX_OPEN_PR_REVIEW_COLLATERAL_MULTIPLIER,
        1.0 + REVIEW_PENALTY_RATE * changes_requested_count,
    )
    if changes_requested_count > 0:
        ctx = f' (PR #{pr_number})' if pr_number else ''
        bt.logging.info(
            f'{changes_requested_count} maintainer CHANGES_REQUESTED review(s){ctx} → '
            f'review_collateral_multiplier={multiplier:.2f}'
        )
    return multiplier


def calculate_open_pr_threshold(total_token_score: float = 0.0) -> int:
    """Calculate dynamic open PR threshold based on total token score.

    Bonus = floor(total_token_score / OPEN_PR_THRESHOLD_TOKEN_SCORE)
    Threshold = min(BASE_THRESHOLD + bonus, MAX_OPEN_PR_THRESHOLD)
    """
    bonus = int(total_token_score // OPEN_PR_THRESHOLD_TOKEN_SCORE)
    return min(EXCESSIVE_PR_PENALTY_BASE_THRESHOLD + bonus, MAX_OPEN_PR_THRESHOLD)


def calculate_pr_spam_penalty_multiplier(total_open_prs: int, total_token_score: float = 0.0) -> float:
    """Apply penalty for excessive open PRs.

    Binary multiplier: 1.0 if open PRs <= threshold, 0.0 otherwise.
    """
    threshold = calculate_open_pr_threshold(total_token_score)
    return 1.0 if total_open_prs <= threshold else 0.0


def _pr_bypasses_eligibility(
    pr: 'ScoredPR',
    master_repositories: Dict[str, RepositoryConfig],
) -> bool:
    repo_config = master_repositories.get(pr.repository_full_name)
    return repo_config is not None and not repo_config.eligibility_mode


def finalize_miner_scores(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Optional[Dict[str, RepositoryConfig]] = None,
) -> None:
    """Finalize all miner scores: compute earned_scores, then collateral, then aggregate."""
    bt.logging.info('**Finalizing miner scores**')
    master_repositories = master_repositories or {}

    # Phase 1: Compute all earned_scores (base × multipliers) for every miner
    for uid, evaluation in miner_evaluations.items():
        if not evaluation:
            continue

        bt.logging.info('')
        bt.logging.info('=' * 50)
        bt.logging.info(f'UID {uid}')
        bt.logging.info('=' * 50)

        # Compute collateral on each open PR; defer accumulation so eligibility-disabled
        # repos don't reduce gated repo rewards (and vice versa).
        for pr in evaluation.open_prs:
            pr.collateral_score = calculate_open_pr_collateral_score(pr)

        has_contributions = evaluation.total_merged_prs > 0 or evaluation.total_closed_prs > 0

        if not has_contributions:
            evaluation.total_collateral_score += sum(pr.collateral_score for pr in evaluation.open_prs)
            bt.logging.info('No merged or closed PRs - skipping evaluation')
            continue

        gated_open_prs = [pr for pr in evaluation.open_prs if not _pr_bypasses_eligibility(pr, master_repositories)]
        bypass_open_prs = [pr for pr in evaluation.open_prs if _pr_bypasses_eligibility(pr, master_repositories)]
        gated_merged_prs = [pr for pr in evaluation.merged_prs if not _pr_bypasses_eligibility(pr, master_repositories)]
        gated_closed_prs = [pr for pr in evaluation.closed_prs if not _pr_bypasses_eligibility(pr, master_repositories)]
        bypass_merged_prs = [pr for pr in evaluation.merged_prs if _pr_bypasses_eligibility(pr, master_repositories)]

        # Check eligibility for the gated portfolio only. eligibility_mode=false
        # PRs must neither unlock nor penalize repos where the gate still applies.
        is_eligible, credibility, reason = check_eligibility(gated_merged_prs, gated_closed_prs)
        evaluation.is_eligible = is_eligible
        evaluation.credibility = credibility

        scorable_prs: list['ScoredPR'] = []
        if is_eligible:
            scorable_prs.extend(gated_merged_prs)
        elif bypass_merged_prs:
            bt.logging.info(
                f'UID {uid} ineligible: {reason} — scoring '
                f'{len(bypass_merged_prs)} PR(s) from eligibility_mode=false repos'
            )
        else:
            bt.logging.info(f'UID {uid} ineligible: {reason} — score set to 0')
            evaluation.total_collateral_score += sum(pr.collateral_score for pr in gated_open_prs)
            continue
        scorable_prs.extend(bypass_merged_prs)

        gated_token_score = sum(pr.token_score for pr in gated_merged_prs)
        gated_spam_multiplier = calculate_pr_spam_penalty_multiplier(len(gated_open_prs), gated_token_score)

        # Bypass spam math intentionally uses total_open_prs (one-way isolation): gated
        # open PRs penalize bypass earnings so eligibility-disabled repos can't be used
        # as a spam-vector escape hatch.
        bypass_token_score = sum(pr.token_score for pr in bypass_merged_prs)
        bypass_spam_multiplier = calculate_pr_spam_penalty_multiplier(evaluation.total_open_prs, bypass_token_score)

        if is_eligible:
            evaluation.total_collateral_score += sum(pr.collateral_score for pr in gated_open_prs)
        if bypass_merged_prs:
            evaluation.total_collateral_score += sum(pr.collateral_score for pr in bypass_open_prs)

        credibility_multiplier = round(credibility, 2)

        for pr in scorable_prs:
            bypasses_gate = _pr_bypasses_eligibility(pr, master_repositories)
            pr.open_pr_spam_multiplier = bypass_spam_multiplier if bypasses_gate else gated_spam_multiplier
            pr.credibility_multiplier = 1.0 if bypasses_gate else credibility_multiplier
            pr.calculate_final_earned_score()

            evaluation.total_token_score += pr.token_score
            evaluation.total_structural_count += pr.structural_count
            evaluation.total_structural_score += pr.structural_score
            evaluation.total_leaf_count += pr.leaf_count
            evaluation.total_leaf_score += pr.leaf_score

    # Phase 2: Aggregate totals, apply collateral, log summary
    for uid, evaluation in miner_evaluations.items():
        if not evaluation:
            continue

        has_contributions = evaluation.total_merged_prs > 0 or evaluation.total_closed_prs > 0
        if not has_contributions:
            continue

        for pr in evaluation.merged_prs:
            evaluation.base_total_score += pr.base_score
            evaluation.total_score += pr.earned_score
            evaluation.total_nodes_scored += pr.total_nodes_scored

        earned_score = evaluation.total_score
        evaluation.total_score = max(0.0, earned_score - evaluation.total_collateral_score)
        evaluation.unique_repos_count = len(evaluation.unique_repos_contributed_to)

        bt.logging.info('')
        bt.logging.info('Summary:')
        bt.logging.info(
            f'├─ Score: {earned_score:.2f} - {evaluation.total_collateral_score:.2f} collateral = {evaluation.total_score:.2f}'
        )
        bt.logging.info(
            f'├─ PRs: {evaluation.total_merged_prs} merged | {evaluation.total_open_prs} open | {evaluation.total_closed_prs} closed'
        )
        bt.logging.info(f'└─ Eligible: {evaluation.is_eligible} | Credibility: {evaluation.credibility:.2f}')

    bt.logging.info('Finalization complete.')


def calculate_open_pr_collateral_score(pr: 'ScoredPR') -> float:
    """
    Calculate collateral score for an open PR.

    Collateral = base_score * applicable_multipliers * OPEN_PR_COLLATERAL_PERCENT

    Applicable multipliers: issue, label, review_collateral
    NOT applicable: time_decay (merge-based), credibility_multiplier (merge-based),
                    open_pr_spam (not for collateral)
    """
    from math import prod

    multipliers = {
        'issue': pr.issue_multiplier,
        'label': pr.label_multiplier,
        'review_collateral': calculate_review_collateral_multiplier(pr.changes_requested_count, pr.number),
    }

    potential_score = pr.base_score * prod(multipliers.values())
    collateral_score = potential_score * OPEN_PR_COLLATERAL_PERCENT

    mult_str = ' | '.join([f'{k}: {v:.2f}' for k, v in multipliers.items()])
    bt.logging.info(
        f'OPEN PR #{pr.number} | base: {pr.base_score:.2f} | {mult_str} | '
        f'potential: {potential_score:.2f} | collateral ({OPEN_PR_COLLATERAL_PERCENT * 100:.0f}%): {collateral_score:.2f}'
    )

    return collateral_score
