"""Unit tests for calculate_base_score_for_pr_files — the shared helper used
by both OSS per-PR scoring and mirror issue discovery base-score resolution.
"""

import pytest

scoring_module = pytest.importorskip(
    'gittensor.validator.oss_contributions.mirror.scoring',
    reason='Requires gittensor mirror subpackage',
)
load_weights = pytest.importorskip('gittensor.validator.utils.load_weights')

calculate_base_score_for_pr_files = scoring_module.calculate_base_score_for_pr_files
BaseScoreResult = scoring_module.BaseScoreResult
TokenConfig = load_weights.TokenConfig


class TestEmptyInput:
    def test_empty_file_changes_returns_zero_result(self):
        result = calculate_base_score_for_pr_files(
            file_changes=[],
            file_contents={},
            programming_languages={},
            token_config=TokenConfig(),
        )
        assert isinstance(result, BaseScoreResult)
        assert result.base_score == 0.0
        assert result.token_score == 0.0
        assert result.total_nodes_scored == 0
        assert result.structural_count == 0
        assert result.leaf_count == 0
        assert result.code_density == 0.0


class TestResultShape:
    def test_result_has_all_expected_fields(self):
        result = calculate_base_score_for_pr_files(
            file_changes=[],
            file_contents={},
            programming_languages={},
            token_config=TokenConfig(),
        )
        # Verify every documented field is present
        for field_name in [
            'base_score', 'token_score',
            'structural_count', 'structural_score',
            'leaf_count', 'leaf_score',
            'total_nodes_scored', 'code_density',
        ]:
            assert hasattr(result, field_name), f'missing {field_name}'
