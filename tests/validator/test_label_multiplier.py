import pytest

from gittensor.validator.oss_contributions.scoring import _get_label_multiplier


class TestGetLabelMultiplier:
    @pytest.mark.parametrize('label', ['feature', 'feat'])
    def test_feature_keywords(self, label):
        assert _get_label_multiplier(label) == 1.50

    @pytest.mark.parametrize('label', ['bug', 'fix', 'crash', 'regression', 'security', 'perf'])
    def test_bug_keywords(self, label):
        assert _get_label_multiplier(label) == 1.25

    @pytest.mark.parametrize('label', ['enhancement', 'improve'])
    def test_enhancement_keywords(self, label):
        assert _get_label_multiplier(label) == 1.10

    @pytest.mark.parametrize('label', ['refactor', 'cleanup', 'debt', 'chore', 'polish'])
    def test_refactor_keywords(self, label):
        assert _get_label_multiplier(label) == 1.00

    @pytest.mark.parametrize(
        'label,expected',
        [
            ('🐞 bug', 1.25),
            ('kind/feature', 1.50),
        ],
    )
    def test_prefixed_labels(self, label, expected):
        # Substring matching works with prefixes and emoji
        assert _get_label_multiplier(label) == expected

    @pytest.mark.parametrize('label', ['docs', 'question'])
    def test_unrecognized_label_returns_default(self, label):
        # No match defaults to 1.0
        assert _get_label_multiplier(label) == 1.0

    def test_wontfix_matches_fix_keyword(self):
        # false-positive, but likely wont be merged anyway
        assert _get_label_multiplier('wontfix') == 1.25
