# The MIT License (MIT)
# Copyright © 2025 Entrius

from datetime import datetime, timezone
from unittest.mock import Mock, patch

from gittensor.classes import MinerEvaluation, PRState, PullRequest
from gittensor.validator.oss_contributions.scoring import score_miner_prs, score_pull_request


def _make_pr(number: int) -> PullRequest:
    now = datetime.now(timezone.utc)
    return PullRequest(
        number=number,
        repository_full_name='owner/repo',
        uid=1,
        hotkey='hk',
        github_id='1',
        title=f'PR {number}',
        author_login='miner',
        merged_at=now,
        created_at=now,
        pr_state=PRState.MERGED,
    )


@patch('gittensor.validator.oss_contributions.scoring.get_pull_request_file_changes')
def test_score_pull_request_skips_on_file_changes_none(mock_get_file_changes):
    """When get_pull_request_file_changes returns None (REST failure), the PR is skipped."""
    mock_get_file_changes.return_value = None

    pr = _make_pr(10)
    miner_eval = MinerEvaluation(uid=1, hotkey='hk', github_id='1', github_pat='fake_pat')

    score_pull_request(
        pr,
        miner_eval,
        master_repositories={'owner/repo': Mock(weight=1.0, inactive_at=None)},
        programming_languages={},
        token_config=Mock(),
    )

    assert miner_eval.github_pr_fetch_failed is False
    assert pr.base_score == 0.0


@patch('gittensor.validator.oss_contributions.scoring.get_pull_request_file_changes')
def test_score_pull_request_skips_empty_file_changes_without_fetch_failed(mock_get_file_changes):
    """An empty file-change list (not None) should skip scoring but NOT set fetch_failed."""
    mock_get_file_changes.return_value = []

    pr = _make_pr(11)
    miner_eval = MinerEvaluation(uid=1, hotkey='hk', github_id='1', github_pat='fake_pat')

    score_pull_request(
        pr,
        miner_eval,
        master_repositories={'owner/repo': Mock(weight=1.0, inactive_at=None)},
        programming_languages={},
        token_config=Mock(),
    )

    assert miner_eval.github_pr_fetch_failed is False
    assert pr.base_score == 0.0


@patch('gittensor.validator.oss_contributions.scoring.fetch_file_contents_for_pr')
@patch('gittensor.validator.oss_contributions.scoring.get_pull_request_file_changes')
def test_score_pull_request_skips_on_graphql_content_fetch_failure(mock_get_file_changes, mock_fetch_contents):
    """When GraphQL file-content fetch fails, the PR is skipped without setting miner-level flag."""
    mock_get_file_changes.return_value = [Mock(filename='test.py', status='modified', changes=5)]
    mock_fetch_contents.return_value = ({}, True)

    pr = _make_pr(12)
    miner_eval = MinerEvaluation(uid=1, hotkey='hk', github_id='1', github_pat='fake_pat')

    score_pull_request(
        pr,
        miner_eval,
        master_repositories={'owner/repo': Mock(weight=1.0, inactive_at=None)},
        programming_languages={},
        token_config=Mock(),
    )

    assert miner_eval.github_pr_fetch_failed is False
    assert pr.base_score == 0.0


@patch('gittensor.validator.oss_contributions.scoring.score_pull_request')
def test_score_miner_prs_continues_after_per_pr_failure(mock_score_pull_request):
    """A per-PR fetch failure should NOT prevent scoring subsequent PRs."""
    miner_eval = MinerEvaluation(uid=1, hotkey='hk', github_id='1', github_pat='fake_pat')
    miner_eval.merged_pull_requests = [_make_pr(1), _make_pr(2)]
    miner_eval.open_pull_requests = [_make_pr(3)]

    score_miner_prs(
        miner_eval=miner_eval,
        master_repositories={},
        programming_languages={},
        token_config=Mock(),
    )

    assert mock_score_pull_request.call_count == 3
    assert miner_eval.github_pr_fetch_failed is False
