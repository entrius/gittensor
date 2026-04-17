# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Unit tests for FileScoreResult.category and PrScoringResult.density"""

import pytest

from gittensor.classes import FileScoreResult, PrScoringResult, ScoringCategory
from gittensor.constants import MAX_CODE_DENSITY_MULTIPLIER


def _file_result(is_test_file: bool = False, scoring_method: str = 'tree-diff') -> FileScoreResult:
    return FileScoreResult(
        filename='f.py',
        score=1.0,
        nodes_scored=1,
        total_lines=10,
        is_test_file=is_test_file,
        scoring_method=scoring_method,
    )


class TestFileScoreResultCategory:
    def test_tree_diff_non_test_is_source(self):
        assert _file_result(is_test_file=False, scoring_method='tree-diff').category == ScoringCategory.SOURCE

    def test_test_file_tree_diff_is_test(self):
        assert _file_result(is_test_file=True, scoring_method='tree-diff').category == ScoringCategory.TEST

    def test_test_file_line_count_is_test(self):
        assert _file_result(is_test_file=True, scoring_method='line-count').category == ScoringCategory.TEST

    def test_test_file_skipped_is_test(self):
        assert _file_result(is_test_file=True, scoring_method='skipped').category == ScoringCategory.TEST

    def test_line_count_non_test_is_non_code(self):
        assert _file_result(is_test_file=False, scoring_method='line-count').category == ScoringCategory.NON_CODE

    def test_skipped_is_non_code(self):
        assert _file_result(is_test_file=False, scoring_method='skipped').category == ScoringCategory.NON_CODE

    def test_skipped_binary_is_non_code(self):
        assert _file_result(is_test_file=False, scoring_method='skipped-binary').category == ScoringCategory.NON_CODE

    def test_skipped_large_is_non_code(self):
        assert _file_result(is_test_file=False, scoring_method='skipped-large').category == ScoringCategory.NON_CODE

    def test_skipped_unsupported_is_non_code(self):
        assert (
            _file_result(is_test_file=False, scoring_method='skipped-unsupported').category == ScoringCategory.NON_CODE
        )

    def test_is_test_takes_priority_over_scoring_method(self):
        """is_test_file=True always routes to TEST regardless of scoring_method"""
        for method in ('tree-diff', 'line-count', 'skipped', 'skipped-binary'):
            assert _file_result(is_test_file=True, scoring_method=method).category == ScoringCategory.TEST


def _pr_result(total_score: float, total_lines: int) -> PrScoringResult:
    return PrScoringResult(
        total_score=total_score,
        total_nodes_scored=0,
        total_lines=total_lines,
        total_additions=total_lines,
        total_deletions=0,
        file_results=[],
    )


class TestPrScoringResultDensity:
    def test_basic_density(self):
        assert _pr_result(total_score=10.0, total_lines=20).density == pytest.approx(0.5)

    def test_zero_lines_returns_zero(self):
        assert _pr_result(total_score=10.0, total_lines=0).density == 0.0

    def test_negative_lines_returns_zero(self):
        assert _pr_result(total_score=10.0, total_lines=-1).density == 0.0

    def test_capped_at_max(self):
        """Density is capped at MAX_CODE_DENSITY_MULTIPLIER even if ratio is higher"""
        result = _pr_result(total_score=100.0, total_lines=1)
        assert result.density == MAX_CODE_DENSITY_MULTIPLIER

    def test_exactly_at_cap(self):
        result = _pr_result(total_score=MAX_CODE_DENSITY_MULTIPLIER * 10, total_lines=10)
        assert result.density == pytest.approx(MAX_CODE_DENSITY_MULTIPLIER)

    def test_just_below_cap(self):
        score = (MAX_CODE_DENSITY_MULTIPLIER - 0.01) * 10
        result = _pr_result(total_score=score, total_lines=10)
        assert result.density == pytest.approx(score / 10)
        assert result.density < MAX_CODE_DENSITY_MULTIPLIER

    def test_zero_score_returns_zero(self):
        assert _pr_result(total_score=0.0, total_lines=10).density == 0.0
