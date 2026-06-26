# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Unit tests for FileScoreResult.category"""

from gittensor.classes import FileScoreResult, ScoringCategory


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
