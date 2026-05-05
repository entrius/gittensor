from datetime import datetime, timezone
from typing import Optional

from gittensor.classes import FileChange, PRState, PullRequest
from gittensor.constants import MIN_TOKEN_SCORE_FOR_BASE_SCORE, MIN_VALID_MERGED_PRS
from gittensor.utils.github_api_tools import FileContentPair
from gittensor.validator.oss_contributions.credibility import check_eligibility
from gittensor.validator.oss_contributions.mirror.scoring import calculate_base_score_for_pr_files
from gittensor.validator.utils.load_weights import load_programming_language_weights, load_token_config


def _generated_python_functions(prefix: str, count: int) -> str:
    return '\n'.join(
        f'def {prefix}_{i}():\n    value = {i}\n    adjusted = value + 1\n    return adjusted\n' for i in range(count)
    )


def _score_single_file(filename: str, content: str):
    lines = len(content.splitlines())
    change = FileChange(
        pr_number=1,
        repository_full_name='test/repo',
        filename=filename,
        changes=lines,
        additions=lines,
        deletions=0,
        status='added',
    )
    return calculate_base_score_for_pr_files(
        [change],
        {filename: FileContentPair(old_content=None, new_content=content)},
        load_programming_language_weights(),
        load_token_config(),
    )


def _merged_pr(token_score: float, source_token_score: Optional[float]) -> PullRequest:
    return PullRequest(
        number=1,
        repository_full_name='test/repo',
        uid=1,
        hotkey='hk',
        github_id='gh',
        title='t',
        author_login='author',
        merged_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        pr_state=PRState.MERGED,
        token_score=token_score,
        source_token_score=source_token_score,
    )


def test_test_only_tree_score_does_not_satisfy_merged_pr_eligibility():
    result = _score_single_file('tests/test_alpha.py', _generated_python_functions('test_alpha', 80))
    source_token_score = getattr(result, 'source_token_score', 0.0)

    prs = [_merged_pr(result.token_score, source_token_score) for _ in range(MIN_VALID_MERGED_PRS)]
    is_eligible, credibility, reason = check_eligibility(prs, [])

    assert result.token_score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE
    assert result.base_score < 1.0
    assert result.code_density == 0.0
    assert source_token_score == 0.0
    assert is_eligible is False
    assert credibility == 1.0
    assert reason == f'0/{MIN_VALID_MERGED_PRS} valid merged PRs (need {MIN_VALID_MERGED_PRS})'


def test_source_tree_score_still_satisfies_merged_pr_eligibility():
    result = _score_single_file('src/alpha.py', _generated_python_functions('alpha', 20))
    source_token_score = result.source_token_score

    prs = [_merged_pr(result.token_score, source_token_score) for _ in range(MIN_VALID_MERGED_PRS)]
    is_eligible, credibility, reason = check_eligibility(prs, [])

    assert source_token_score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE
    assert result.code_density > 0.0
    assert is_eligible is True
    assert credibility == 1.0
    assert reason == ''


def test_missing_source_token_score_falls_back_to_token_score_for_compatibility():
    prs = [_merged_pr(MIN_TOKEN_SCORE_FOR_BASE_SCORE, None) for _ in range(MIN_VALID_MERGED_PRS)]

    is_eligible, credibility, reason = check_eligibility(prs, [])

    assert is_eligible is True
    assert credibility == 1.0
    assert reason == ''
