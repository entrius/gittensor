# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Tests for the open PR collateral system: calculate_open_pr_collateral_score

Covers:
- Basic collateral calculation (base_score * multipliers * collateral_percent)
- Missing tier configuration handling
- Zero base score edge case
- Multiplier application (repo_weight, issue)
- Collateral percentage from tier config

Run tests:
    pytest tests/validator/test_collateral.py -v
"""

from datetime import datetime, timezone
from typing import Optional

import pytest

from gittensor.classes import PRState, PullRequest
from gittensor.constants import DEFAULT_COLLATERAL_PERCENT
from gittensor.validator.configurations.tier_config import TIERS, Tier, TierConfig
from gittensor.validator.evaluation.scoring import calculate_open_pr_collateral_score


# ============================================================================
# Helper
# ============================================================================

def _make_open_pr(
    *,
    number: int = 1,
    base_score: float = 10.0,
    repo_weight: float = 1.0,
    issue_multiplier: float = 1.0,
    tier_config: Optional[TierConfig] = None,
    repo: str = "owner/repo",
) -> PullRequest:
    pr = PullRequest(
        number=number,
        repository_full_name=repo,
        uid=0,
        hotkey="test_hotkey",
        github_id="12345",
        title=f"Open PR #{number}",
        author_login="contributor",
        merged_at=None,
        created_at=datetime.now(timezone.utc),
        pr_state=PRState.OPEN,
        repository_tier_configuration=tier_config,
        base_score=base_score,
        repo_weight_multiplier=repo_weight,
        issue_multiplier=issue_multiplier,
    )
    return pr


# ============================================================================
# Tests
# ============================================================================


class TestCalculateOpenPrCollateralScore:
    """Tests for open PR collateral score calculation."""

    def test_basic_collateral(self):
        """Collateral = base_score * repo_weight * issue_mult * collateral_percent."""
        tier = TIERS[Tier.BRONZE]
        pr = _make_open_pr(base_score=100.0, repo_weight=0.5, issue_multiplier=1.2, tier_config=tier)
        result = calculate_open_pr_collateral_score(pr)
        expected = 100.0 * 0.5 * 1.2 * tier.open_pr_collateral_percentage
        assert abs(result - expected) < 0.01

    def test_missing_tier_config_returns_zero(self):
        """PR with no tier configuration should return 0 collateral."""
        pr = _make_open_pr(tier_config=None, base_score=100.0)
        result = calculate_open_pr_collateral_score(pr)
        assert result == 0.0

    def test_zero_base_score(self):
        """PR with zero base score should return 0 collateral."""
        tier = TIERS[Tier.BRONZE]
        pr = _make_open_pr(base_score=0.0, tier_config=tier)
        result = calculate_open_pr_collateral_score(pr)
        assert result == 0.0

    def test_default_multipliers(self):
        """With default multipliers (1.0), collateral is base * collateral_percent."""
        tier = TIERS[Tier.BRONZE]
        pr = _make_open_pr(base_score=50.0, tier_config=tier)
        result = calculate_open_pr_collateral_score(pr)
        expected = 50.0 * tier.open_pr_collateral_percentage
        assert abs(result - expected) < 0.01

    def test_collateral_uses_correct_percentage(self):
        """Each tier should use its own collateral percentage."""
        for tier_name in [Tier.BRONZE, Tier.SILVER, Tier.GOLD]:
            tier = TIERS[tier_name]
            pr = _make_open_pr(base_score=100.0, tier_config=tier)
            result = calculate_open_pr_collateral_score(pr)
            expected = 100.0 * tier.open_pr_collateral_percentage
            assert abs(result - expected) < 0.01, f"Failed for {tier_name}"

    def test_high_issue_multiplier_increases_collateral(self):
        """Higher issue multiplier should increase collateral proportionally."""
        tier = TIERS[Tier.BRONZE]
        pr_low = _make_open_pr(base_score=100.0, issue_multiplier=1.0, tier_config=tier)
        pr_high = _make_open_pr(base_score=100.0, issue_multiplier=2.0, tier_config=tier)
        result_low = calculate_open_pr_collateral_score(pr_low)
        result_high = calculate_open_pr_collateral_score(pr_high)
        assert abs(result_high - result_low * 2) < 0.01

    def test_repo_weight_scales_collateral(self):
        """Repo weight should scale collateral proportionally."""
        tier = TIERS[Tier.BRONZE]
        pr = _make_open_pr(base_score=100.0, repo_weight=0.25, tier_config=tier)
        result = calculate_open_pr_collateral_score(pr)
        expected = 100.0 * 0.25 * tier.open_pr_collateral_percentage
        assert abs(result - expected) < 0.01
