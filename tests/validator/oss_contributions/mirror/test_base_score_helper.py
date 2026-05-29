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
    nothing — leaving the SOURCE threshold gate as the only source of base_score.
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
            'base_score',
            'token_score',
            'source_token_score',
            'structural_count',
            'structural_score',
            'leaf_count',
            'leaf_score',
            'total_nodes_scored',
            'code_density',
        ]:
            assert hasattr(result, field_name), f'missing {field_name}'


class TestPerRepoMinTokenScoreOverride:
    """Per-repo ``min_token_score_for_base_score`` must gate the PR base score.

    Regression guard: previously the helper hardcoded the global
    ``MIN_TOKEN_SCORE_FOR_BASE_SCORE`` constant, so per-repo eligibility overrides
    introduced by #1293 were silently ignored on the PR-scoring path while
    issue-discovery honored them — a state asymmetry between paths.
    """

    def _patch_scorer(self, monkeypatch, source_token_score: float):
        monkeypatch.setattr(
            scoring_module,
            'calculate_token_score_from_file_changes',
            lambda *args, **kwargs: _stub_scoring_result(source_token_score),
        )

    def test_default_threshold_applies_when_none_passed(self, monkeypatch):
        # source_token_score = 4 is below the global default (5) → base_score == 0
        self._patch_scorer(monkeypatch, source_token_score=4.0)
        result = calculate_base_score_for_pr_files(
            file_changes=[],
            file_contents={},
            programming_languages={},
            token_config=TokenConfig(),
        )
        assert result.base_score == 0.0

    def test_lower_per_repo_threshold_lets_pr_score(self, monkeypatch):
        # Same token_score = 4, but a permissive per-repo override (3) should let it score.
        self._patch_scorer(monkeypatch, source_token_score=4.0)
        result = calculate_base_score_for_pr_files(
            file_changes=[],
            file_contents={},
            programming_languages={},
            token_config=TokenConfig(),
            min_token_score_for_base_score=3.0,
        )
        assert result.base_score > 0.0
        assert result.source_token_score == pytest.approx(4.0)

    def test_higher_per_repo_threshold_zeroes_pr(self, monkeypatch):
        # token_score = 7 would pass default (5), but stricter per-repo override (10) zeroes it.
        self._patch_scorer(monkeypatch, source_token_score=7.0)
        result = calculate_base_score_for_pr_files(
            file_changes=[],
            file_contents={},
            programming_languages={},
            token_config=TokenConfig(),
            min_token_score_for_base_score=10.0,
        )
        assert result.base_score == 0.0
