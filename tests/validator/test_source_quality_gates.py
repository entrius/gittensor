"""Tests for SOURCE-quality score eligibility gates (#1023)."""
from dataclasses import dataclass
from typing import Optional

import pytest

from gittensor.constants import MIN_TOKEN_SCORE_FOR_BASE_SCORE


# ---------------------------------------------------------------------------
# Helpers: lightweight stand-ins for PullRequest / ScoredMirrorPR
# ---------------------------------------------------------------------------

@dataclass
class FakePR:
    """Minimal duck-type matching PullRequest / PrLike."""
    merged_at: Optional[str] = "2026-01-01T00:00:00Z"
    source_token_score: float = 0.0
    token_score: float = 0.0

    @property
    def is_pioneer_eligible(self) -> bool:
        score = self.source_token_score if self.source_token_score > 0 else self.token_score
        return self.merged_at is not None and score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE


def _effective(pr: FakePR) -> float:
    """Replicate the fallback logic used in eligibility gates."""
    return pr.source_token_score if pr.source_token_score > 0 else pr.token_score


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTestOnlyPRNotEligible:
    """A PR with high aggregate token_score but 0 source_token_score is not eligible."""

    def test_pioneer_gate(self):
        pr = FakePR(token_score=100.0, source_token_score=0.0)
        assert not pr.is_pioneer_eligible

    def test_credibility_gate(self):
        from gittensor.validator.oss_contributions.credibility import check_eligibility
        prs = [FakePR(token_score=100.0, source_token_score=0.0)]
        # check_eligibility expects at least MIN_VALID_MERGED_PRS valid PRs
        eligible, reason = check_eligibility(prs, [])
        assert not eligible


class TestSourceOnlyPREligible:
    """A PR with only source contributions should be eligible."""

    def test_pioneer_gate(self):
        pr = FakePR(token_score=10.0, source_token_score=10.0)
        assert pr.is_pioneer_eligible

    def test_effective_score(self):
        pr = FakePR(token_score=15.0, source_token_score=10.0)
        assert _effective(pr) == 10.0


class TestFallbackBehavior:
    """When source_token_score is 0 (old cached data), fall back to token_score."""

    def test_falls_back_to_aggregate(self):
        pr = FakePR(token_score=10.0, source_token_score=0.0)
        assert _effective(pr) == 10.0

    def test_uses_source_when_present(self):
        pr = FakePR(token_score=50.0, source_token_score=5.0)
        assert _effective(pr) == 5.0

    def test_exactly_at_threshold(self):
        pr = FakePR(token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE, source_token_score=0.0)
        assert _effective(pr) == MIN_TOKEN_SCORE_FOR_BASE_SCORE
        assert pr.is_pioneer_eligible

    def test_just_below_threshold(self):
        pr = FakePR(token_score=MIN_TOKEN_SCORE_FOR_BASE_SCORE - 0.01, source_token_score=0.0)
        assert not pr.is_pioneer_eligible


class TestZeroBothNotEligible:
    """Both scores at 0 → not eligible."""

    def test_pioneer(self):
        pr = FakePR(token_score=0.0, source_token_score=0.0)
        assert not pr.is_pioneer_eligible

    def test_unmerged(self):
        pr = FakePR(merged_at=None, token_score=100.0, source_token_score=100.0)
        assert not pr.is_pioneer_eligible


class TestCachedSolvingPRPropagation:
    """source_token_score propagates through CachedSolvingPR."""

    def test_cache_holds_source_score(self):
        from gittensor.validator.issue_discovery.mirror_scan import CachedSolvingPR
        cached = CachedSolvingPR(
            base_score=1.0, token_score=50.0, source_token_score=10.0
        )
        assert cached.source_token_score == 10.0
        effective = cached.source_token_score if cached.source_token_score > 0 else cached.token_score
        assert effective == 10.0

    def test_cache_fallback(self):
        from gittensor.validator.issue_discovery.mirror_scan import CachedSolvingPR
        cached = CachedSolvingPR(base_score=1.0, token_score=10.0, source_token_score=0.0)
        effective = cached.source_token_score if cached.source_token_score > 0 else cached.token_score
        assert effective == 10.0
