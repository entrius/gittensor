"""
Tests for the shared normalize_scores utility and its callers.

Run tests:
    pytest tests/validator/test_normalize.py -v
"""

from unittest.mock import MagicMock

from gittensor.validator.utils.normalize import normalize_scores


class TestNormalizeScores:
    """Tests for the shared normalize_scores function."""

    def test_empty_dict_returns_empty(self):
        """Empty input returns empty dict."""
        assert normalize_scores({}) == {}

    def test_single_miner_normalizes_to_one(self):
        """A single positive score normalizes to 1.0."""
        result = normalize_scores({0: 5.0})
        assert result == {0: 1.0}

    def test_equal_scores_normalize_evenly(self):
        """Equal scores produce equal normalized values."""
        result = normalize_scores({0: 10.0, 1: 10.0})
        assert result == {0: 0.5, 1: 0.5}

    def test_unequal_scores_preserve_ratios(self):
        """Unequal scores preserve their original ratios."""
        result = normalize_scores({0: 30.0, 1: 10.0})
        assert result[0] == 0.75
        assert result[1] == 0.25

    def test_sum_equals_one(self):
        """Normalized scores sum to 1.0."""
        result = normalize_scores({0: 3.0, 1: 7.0, 2: 5.0})
        assert abs(sum(result.values()) - 1.0) < 1e-9

    def test_all_zeros_returns_original(self):
        """All-zero scores are returned unchanged."""
        scores = {0: 0.0, 1: 0.0}
        result = normalize_scores(scores)
        assert result == {0: 0.0, 1: 0.0}

    def test_mix_of_zero_and_positive(self):
        """Zero-score miners get 0.0 in normalized output."""
        result = normalize_scores({0: 0.0, 1: 10.0})
        assert result[0] == 0.0
        assert result[1] == 1.0

    def test_preserves_uid_keys(self):
        """All original UIDs appear in the output."""
        scores = {5: 1.0, 10: 2.0, 15: 3.0}
        result = normalize_scores(scores)
        assert set(result.keys()) == {5, 10, 15}


class TestNormalizeRewardsLinearDelegation:
    """Verify normalize_rewards_linear delegates to normalize_scores."""

    def test_delegates_normalization(self):
        """normalize_rewards_linear uses normalize_scores for the math."""
        from gittensor.validator.oss_contributions.normalize import normalize_rewards_linear

        eval_a = MagicMock()
        eval_a.total_score = 30.0
        eval_b = MagicMock()
        eval_b.total_score = 10.0

        result = normalize_rewards_linear({0: eval_a, 1: eval_b})
        assert result[0] == 0.75
        assert result[1] == 0.25

    def test_empty_evaluations_returns_empty(self):
        """Empty input returns empty dict."""
        from gittensor.validator.oss_contributions.normalize import normalize_rewards_linear

        assert normalize_rewards_linear({}) == {}

    def test_all_zero_scores_returns_zeros(self):
        """All-zero evaluations return zero scores."""
        from gittensor.validator.oss_contributions.normalize import normalize_rewards_linear

        eval_a = MagicMock()
        eval_a.total_score = 0.0
        eval_b = MagicMock()
        eval_b.total_score = 0.0

        result = normalize_rewards_linear({0: eval_a, 1: eval_b})
        assert result == {0: 0.0, 1: 0.0}


class TestNormalizeIssueDiscoveryRewardsDelegation:
    """Verify normalize_issue_discovery_rewards delegates to normalize_scores."""

    def test_delegates_normalization(self):
        """normalize_issue_discovery_rewards uses normalize_scores for the math."""
        from gittensor.validator.issue_discovery.normalize import normalize_issue_discovery_rewards

        eval_a = MagicMock()
        eval_a.issue_discovery_score = 20.0
        eval_b = MagicMock()
        eval_b.issue_discovery_score = 80.0

        result = normalize_issue_discovery_rewards({0: eval_a, 1: eval_b})
        assert result[0] == 0.2
        assert result[1] == 0.8

    def test_empty_evaluations_returns_empty(self):
        """Empty input returns empty dict."""
        from gittensor.validator.issue_discovery.normalize import normalize_issue_discovery_rewards

        assert normalize_issue_discovery_rewards({}) == {}

    def test_all_zero_scores_returns_zeros(self):
        """All-zero evaluations return zero scores."""
        from gittensor.validator.issue_discovery.normalize import normalize_issue_discovery_rewards

        eval_a = MagicMock()
        eval_a.issue_discovery_score = 0.0
        eval_b = MagicMock()
        eval_b.issue_discovery_score = 0.0

        result = normalize_issue_discovery_rewards({0: eval_a, 1: eval_b})
        assert result == {0: 0.0, 1: 0.0}
