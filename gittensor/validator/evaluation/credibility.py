# The MIT License (MIT)
# Copyright © 2025 Entrius

from typing import TYPE_CHECKING, Dict, List

import bittensor as bt

from gittensor.validator.configurations.tier_config import (
    TIERS,
    TIERS_ORDER,
    Tier,
    TierStats,
    get_tier_from_config,
)

if TYPE_CHECKING:
    from gittensor.classes import PullRequest


def get_tier(pr: 'PullRequest') -> Tier | None:
    if pr.repository_tier_configuration:
        return get_tier_from_config(pr.repository_tier_configuration)
    return None


def calculate_tier_stats(
    merged_prs: List['PullRequest'],
    closed_prs: List['PullRequest'],
    open_prs: List['PullRequest'] = [],
    include_scoring_details: bool = False,
) -> Dict[Tier, TierStats]:
    """Calculate merged/closed counts per tier."""
    from collections import defaultdict

    stats: Dict[Tier, TierStats] = {tier: TierStats() for tier in Tier}
    repos_per_tier: Dict[Tier, set] = {tier: set() for tier in Tier}
    # Track token scores per repository per tier
    repo_token_scores_per_tier: Dict[Tier, Dict[str, float]] = {tier: defaultdict(float) for tier in Tier}

    for pr in merged_prs:
        if tier := get_tier(pr):
            stats[tier].merged_count += 1
            repos_per_tier[tier].add(pr.repository_full_name)
            repo_token_scores_per_tier[tier][pr.repository_full_name] += pr.token_score
            if include_scoring_details:
                stats[tier].earned_score += pr.earned_score
            # Aggregate token scoring breakdown
            stats[tier].token_score += pr.token_score
            stats[tier].structural_count += pr.structural_count
            stats[tier].structural_score += pr.structural_score
            stats[tier].leaf_count += pr.leaf_count
            stats[tier].leaf_score += pr.leaf_score

    for pr in closed_prs:
        if tier := get_tier(pr):
            stats[tier].closed_count += 1

    for pr in open_prs:
        if tier := get_tier(pr):
            stats[tier].open_count += 1
            if include_scoring_details:
                stats[tier].collateral_score += pr.collateral_score

    for tier in TIERS_ORDER:
        stats[tier].unique_repo_contribution_count = len(repos_per_tier[tier])
        # Calculate qualified repos based on tier's min token score per repo requirement
        config = TIERS[tier]
        if config.required_min_token_score_per_repo is not None:
            qualified_count = sum(
                1
                for score in repo_token_scores_per_tier[tier].values()
                if score >= config.required_min_token_score_per_repo
            )
            stats[tier].qualified_unique_repo_count = qualified_count
        else:
            # If no min token score per repo required, all unique repos qualify
            stats[tier].qualified_unique_repo_count = len(repos_per_tier[tier])

    return stats


def is_tier_unlocked(tier: Tier, tier_stats: Dict[Tier, TierStats], log_reasons: bool = True) -> bool:
    """
    Check if a tier is unlocked by verifying this tier and all below meet their own requirements.

    Each tier's requirements define what's needed to maintain THAT tier.

    Args:
        tier: The tier to check
        tier_stats: Dictionary of tier statistics
        log_reasons: Whether to log the reason when a tier is locked (default True)
    """
    tier_idx = TIERS_ORDER.index(tier)

    for i in range(tier_idx + 1):  # include current tier
        check_tier = TIERS_ORDER[i]
        config = TIERS[check_tier]
        stats = tier_stats[check_tier]

        if config.required_credibility is not None:
            if stats.credibility < config.required_credibility:
                if log_reasons:
                    bt.logging.info(
                        f'{tier.value} locked: {check_tier.value} needs {config.required_credibility:.2f} credibility, has {stats.credibility:.2f}'
                    )
                return False

        if config.required_min_token_score is not None:
            if stats.token_score < config.required_min_token_score:
                if log_reasons:
                    bt.logging.info(
                        f'{tier.value} locked: {check_tier.value} needs {config.required_min_token_score:.1f} total token score, has {stats.token_score:.1f}'
                    )
                return False

        # Check unique repos with min token score requirement
        if config.required_unique_repos_count is not None:
            if stats.qualified_unique_repo_count < config.required_unique_repos_count:
                if log_reasons:
                    min_score_str = (
                        f' with {config.required_min_token_score_per_repo:.1f}+ token score'
                        if config.required_min_token_score_per_repo
                        else ''
                    )
                    bt.logging.info(
                        f'{tier.value} locked: {check_tier.value} needs {config.required_unique_repos_count} unique repos{min_score_str}, has {stats.qualified_unique_repo_count}'
                    )
                return False

    return True


def calculate_credibility_per_tier(
    merged_prs: List['PullRequest'],
    closed_prs: List['PullRequest'],
) -> Dict[Tier, float]:
    """
    Calculate credibility for each tier, enforcing tier progression.

    Returns dict of tier -> credibility (0.0 if tier not unlocked, else merged/total ratio).
    """
    tier_stats = calculate_tier_stats(merged_prs, closed_prs)
    tier_credibility: Dict[Tier, float] = {}
    tier_display_parts = []

    for tier in Tier:
        stats: TierStats = tier_stats[tier]

        # Check if tier is unlocked (includes checking lower tiers)
        # Suppress logging here - tier unlock reasons are logged in finalize_miner_scores
        tier_unlocked = is_tier_unlocked(tier, tier_stats, log_reasons=False)

        # No activity in this tier
        if stats.total_attempts == 0:
            tier_display_parts.append(f'{tier.value}: LOCKED')
            continue

        # Has activity but tier not unlocked
        if not tier_unlocked:
            tier_credibility[tier] = 0.0
            tier_display_parts.append(f'{tier.value}: LOCKED')
            continue

        # Calculate actual credibility
        credibility = stats.credibility
        tier_credibility[tier] = credibility
        tier_display_parts.append(f'{tier.value}: {stats.merged_count}/{stats.total_attempts} ({credibility:.2f})')

    bt.logging.info(f'Credibility: {" | ".join(tier_display_parts)}')

    return tier_credibility
