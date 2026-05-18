# The MIT License (MIT)
# Copyright © 2025 Entrius

from typing import TYPE_CHECKING, Dict, List, Optional

import bittensor as bt

from gittensor.classes import MinerEvaluation, RepoEvaluation

if TYPE_CHECKING:
    from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR
from gittensor.constants import (
    MAX_OPEN_PR_REVIEW_COLLATERAL_MULTIPLIER,
    OPEN_PR_COLLATERAL_PERCENT,
    REVIEW_PENALTY_RATE,
)
from gittensor.validator.oss_contributions.credibility import check_eligibility
from gittensor.validator.utils.load_weights import RepositoryConfig, ResolvedEligibility, resolve_eligibility


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


def calculate_open_pr_threshold(cfg: ResolvedEligibility, total_token_score: float = 0.0) -> int:
    """Calculate the dynamic open-PR threshold for one repository.

    Bonus = floor(total_token_score / cfg.open_pr_threshold_token_score)
    Threshold = min(cfg.excessive_pr_penalty_base_threshold + bonus, cfg.max_open_pr_threshold)
    """
    bonus = int(total_token_score // cfg.open_pr_threshold_token_score)
    return min(cfg.excessive_pr_penalty_base_threshold + bonus, cfg.max_open_pr_threshold)


def calculate_pr_spam_penalty_multiplier(
    cfg: ResolvedEligibility, total_open_prs: int, total_token_score: float = 0.0
) -> float:
    """Apply the penalty for excessive open PRs within one repository.

    Binary multiplier: 1.0 if the repo's open PRs <= threshold, 0.0 otherwise.
    """
    threshold = calculate_open_pr_threshold(cfg, total_token_score)
    return 1.0 if total_open_prs <= threshold else 0.0


def finalize_miner_scores(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Optional[Dict[str, RepositoryConfig]] = None,
) -> None:
    """Finalize miner scores per repository.

    Each repository gates and scores independently, from only its own PRs,
    against its own resolved eligibility config. Per-repo results land on
    ``evaluation.repo_evaluations`` and roll up into the round-level scalars.
    """
    bt.logging.info('**Finalizing miner scores**')
    master_repositories = master_repositories or {}

    for uid, evaluation in miner_evaluations.items():
        if not evaluation:
            continue

        bt.logging.info('')
        bt.logging.info('=' * 50)
        bt.logging.info(f'UID {uid}')
        bt.logging.info('=' * 50)

        for pr in evaluation.open_prs:
            pr.collateral_score = calculate_open_pr_collateral_score(pr)

        _score_miner_repos(evaluation, master_repositories)
        _roll_up_miner_totals(evaluation)

    bt.logging.info('Finalization complete.')


def _group_prs_by_repo(prs: List['ScoredPR']) -> Dict[str, List['ScoredPR']]:
    grouped: Dict[str, List['ScoredPR']] = {}
    for pr in prs:
        grouped.setdefault(pr.repository_full_name.lower(), []).append(pr)
    return grouped


def _score_miner_repos(
    evaluation: MinerEvaluation,
    master_repositories: Dict[str, RepositoryConfig],
) -> None:
    """Gate and score each repository the miner contributed to, independently."""
    merged_by_repo = _group_prs_by_repo(evaluation.merged_prs)
    open_by_repo = _group_prs_by_repo(evaluation.open_prs)
    closed_by_repo = _group_prs_by_repo(evaluation.closed_prs)

    for repo_name in sorted(set(merged_by_repo) | set(open_by_repo) | set(closed_by_repo)):
        repo_config = master_repositories.get(repo_name)
        if repo_config is None:
            # PRs from untracked repos are dropped upstream; skip defensively.
            continue
        cfg = resolve_eligibility(repo_config.eligibility)
        merged = merged_by_repo.get(repo_name, [])
        open_prs = open_by_repo.get(repo_name, [])
        closed = closed_by_repo.get(repo_name, [])

        repo_eval = RepoEvaluation(
            repository_full_name=repo_name,
            total_merged_prs=len(merged),
            total_open_prs=len(open_prs),
            total_closed_prs=len(closed),
        )
        repo_eval.total_collateral_score = sum(pr.collateral_score for pr in open_prs)
        repo_eval.base_total_score = sum(pr.base_score for pr in merged)
        repo_eval.total_token_score = sum(pr.token_score for pr in merged)
        repo_eval.total_structural_count = sum(pr.structural_count for pr in merged)
        repo_eval.total_structural_score = sum(pr.structural_score for pr in merged)
        repo_eval.total_leaf_count = sum(pr.leaf_count for pr in merged)
        repo_eval.total_leaf_score = sum(pr.leaf_score for pr in merged)
        repo_eval.total_nodes_scored = sum(pr.total_nodes_scored for pr in merged)

        is_eligible, credibility, reason = check_eligibility(merged, closed, cfg)
        repo_eval.is_eligible = is_eligible
        repo_eval.credibility = credibility

        if is_eligible:
            _score_eligible_repo_prs(repo_eval, merged, open_prs, cfg)
        else:
            bt.logging.info(f'├─ {repo_name}: ineligible — {reason}')

        repo_eval.total_score = max(0.0, repo_eval.total_score - repo_eval.total_collateral_score)
        evaluation.repo_evaluations[repo_name] = repo_eval


def _score_eligible_repo_prs(
    repo_eval: RepoEvaluation,
    merged: List['ScoredPR'],
    open_prs: List['ScoredPR'],
    cfg: ResolvedEligibility,
) -> None:
    """Compute earned scores for an eligible repository's merged PRs."""
    spam_multiplier = calculate_pr_spam_penalty_multiplier(cfg, len(open_prs), repo_eval.total_token_score)
    credibility_multiplier = round(repo_eval.credibility, 2)

    for pr in merged:
        pr.open_pr_spam_multiplier = spam_multiplier
        pr.credibility_multiplier = credibility_multiplier
        pr.calculate_final_earned_score()
        repo_eval.total_score += pr.earned_score

    bt.logging.info(
        f'├─ {repo_eval.repository_full_name}: eligible — '
        f'credibility {repo_eval.credibility:.2f}, earned {repo_eval.total_score:.2f}'
    )


def _roll_up_miner_totals(evaluation: MinerEvaluation) -> None:
    """Roll the per-repo evaluations up into the miner's round-level scalars."""
    repo_evals = list(evaluation.repo_evaluations.values())

    evaluation.base_total_score = sum(re.base_total_score for re in repo_evals)
    evaluation.total_score = sum(re.total_score for re in repo_evals)
    evaluation.total_collateral_score = sum(re.total_collateral_score for re in repo_evals)
    evaluation.total_token_score = sum(re.total_token_score for re in repo_evals)
    evaluation.total_structural_count = sum(re.total_structural_count for re in repo_evals)
    evaluation.total_structural_score = sum(re.total_structural_score for re in repo_evals)
    evaluation.total_leaf_count = sum(re.total_leaf_count for re in repo_evals)
    evaluation.total_leaf_score = sum(re.total_leaf_score for re in repo_evals)
    evaluation.total_nodes_scored = sum(re.total_nodes_scored for re in repo_evals)
    evaluation.is_eligible = any(re.is_eligible for re in repo_evals)
    evaluation.credibility = max((re.credibility for re in repo_evals), default=0.0)
    evaluation.unique_repos_count = len(evaluation.unique_repos_contributed_to)

    eligible_repos = sum(1 for re in repo_evals if re.is_eligible)
    bt.logging.info('')
    bt.logging.info('Summary:')
    bt.logging.info(f'├─ Score: {evaluation.total_score:.2f} | collateral {evaluation.total_collateral_score:.2f}')
    bt.logging.info(
        f'├─ PRs: {evaluation.total_merged_prs} merged | {evaluation.total_open_prs} open '
        f'| {evaluation.total_closed_prs} closed'
    )
    bt.logging.info(f'└─ Eligible in {eligible_repos}/{len(repo_evals)} repo(s)')


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
