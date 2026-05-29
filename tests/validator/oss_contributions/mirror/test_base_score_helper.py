"""Unit tests for calculate_base_score_for_pr_files — the shared helper used
by both OSS per-PR scoring and mirror issue discovery base-score resolution.
"""

import pytest

scoring_module = pytest.importorskip(
    'gittensor.validator.oss_contributions.mirror.scoring',
    reason='Requires gittensor mirror subpackage',
)
load_weights = pytest.importorskip('gittensor.validator.utils.load_weights')
classes = pytest.importorskip('gittensor.classes')

calculate_base_score_for_pr_files = scoring_module.calculate_base_score_for_pr_files
BaseScoreResult = scoring_module.BaseScoreResult
TokenConfig = load_weights.TokenConfig
PrScoringResult = classes.PrScoringResult
ScoreBreakdown = classes.ScoreBreakdown
ScoringCategory = classes.ScoringCategory


def _stub_scoring_result(source_token_score: float, total_lines: int = 10) -> PrScoringResult:
    """Build a PrScoringResult whose SOURCE category has a known total_score.

    The outer total_score is zeroed so the contribution_bonus channel contributes
    nothing - leaving the saturation curve as the only source of base_score.
    """
    source_breakdown = ScoreBreakdown(structural_added_score=source_token_score, structural_added_count=1)
    source = PrScoringResult(
        total_score=source_token_score,
        total_nodes_scored=1,
        total_lines=total_lines,
        file_results=[],
        score_breakdown=source_breakdown,
    )
    return PrScoringResult(
        total_score=0.0,
        total_nodes_scored=1,
        total_lines=total_lines,
        file_results=[],
        score_breakdown=source_breakdown,
        by_category={ScoringCategory.SOURCE: source},
    )


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
            'base_score',
            'token_score',
            'structural_count',
            'structural_score',
            'leaf_count',
            'leaf_score',
            'total_nodes_scored',
        ]:
            assert hasattr(result, field_name), f'missing {field_name}'


class TestPerRepoSaturationScaleOverride:
    """Per-repo ``src_tok_saturation_scale`` reshapes the quality curve:
    smaller scale = faster rise, larger scale = slower rise."""

    def _patch_scorer(self, monkeypatch, source_token_score: float):
        monkeypatch.setattr(
            scoring_module,
            'calculate_token_score_from_file_changes',
            lambda *args, **kwargs: _stub_scoring_result(source_token_score),
        )

    def _score(self, **overrides) -> BaseScoreResult:
        return calculate_base_score_for_pr_files(
            file_changes=[],
            file_contents={},
            programming_languages={},
            token_config=TokenConfig(),
            **overrides,
        )

    def test_default_scale_applies_when_none_passed(self, monkeypatch):
        self._patch_scorer(monkeypatch, source_token_score=50.0)
        assert self._score().base_score > 0.0

    def test_smaller_scale_raises_score_for_same_tokens(self, monkeypatch):
        self._patch_scorer(monkeypatch, source_token_score=50.0)
        baseline = self._score()
        permissive = self._score(src_tok_saturation_scale=20.0)
        assert permissive.base_score > baseline.base_score

    def test_larger_scale_lowers_score_for_same_tokens(self, monkeypatch):
        self._patch_scorer(monkeypatch, source_token_score=50.0)
        baseline = self._score()
        strict = self._score(src_tok_saturation_scale=200.0)
        assert strict.base_score < baseline.base_score
        assert strict.base_score > 0.0  # curve is monotonic, never zeroes a real PR
