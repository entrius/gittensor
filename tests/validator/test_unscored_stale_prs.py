# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests for stale-closed PR capture in a storage-only bucket.

Covers the invariant that stale-CLOSED PRs (created before the lookback window):
- do not enter `closed_pull_requests` (preserves the #406 drop-from-scoring)
- do enter `stale_closed_pull_requests` so storage can refresh pr_state
- are not reflected in total counts or scoring buckets
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from gittensor.classes import MinerEvaluation, PRState
from gittensor.utils.github_api_tools import try_add_open_or_closed_pr


def _pr_node(number: int, created_at: str, closed_at: str, state: str = 'CLOSED') -> dict:
    return {
        'number': number,
        'title': f'test PR {number}',
        'state': state,
        'repository': {
            'name': 'gittensor',
            'owner': {'login': 'entrius'},
            'defaultBranchRef': {'name': 'test'},
        },
        'headRepository': {'name': 'gittensor', 'owner': {'login': 'contributor'}},
        'author': {'login': 'contributor'},
        'authorAssociation': 'CONTRIBUTOR',
        'mergedBy': None,
        'mergedAt': None,
        'createdAt': created_at,
        'closedAt': closed_at,
        'lastEditedAt': None,
        'additions': 10,
        'deletions': 5,
        'commits': {'totalCount': 1},
        'baseRefName': 'test',
        'baseRefOid': 'abc',
        'headRefName': 'feature',
        'headRefOid': 'def',
        'bodyText': '',
        'closingIssuesReferences': {'nodes': []},
        'changesRequestedReviews': {'nodes': []},
        'labels': {'nodes': []},
        'timelineItems': {'nodes': []},
    }


@patch('gittensor.utils.github_api_tools.bt.logging')
def test_stale_closed_pr_goes_to_storage_only_bucket(_):
    miner_eval = MinerEvaluation(uid=74, hotkey='hk', github_id='1', github_pat='fake')
    now = datetime.now(timezone.utc)
    lookback = now - timedelta(days=35)
    stale = (lookback - timedelta(days=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    recent_close = (now - timedelta(days=5)).strftime('%Y-%m-%dT%H:%M:%SZ')

    try_add_open_or_closed_pr(miner_eval, _pr_node(1, stale, recent_close), PRState.CLOSED.value, lookback)

    assert len(miner_eval.closed_pull_requests) == 0, 'stale PR must not enter scoring bucket'
    assert len(miner_eval.stale_closed_pull_requests) == 1, 'stale PR must enter storage-only bucket'
    assert miner_eval.stale_closed_pull_requests[0].number == 1
    assert miner_eval.stale_closed_pull_requests[0].pr_state == PRState.CLOSED


@patch('gittensor.utils.github_api_tools.bt.logging')
def test_stale_closed_pr_not_counted_in_totals(_):
    miner_eval = MinerEvaluation(uid=74, hotkey='hk', github_id='1', github_pat='fake')
    now = datetime.now(timezone.utc)
    lookback = now - timedelta(days=35)
    stale = (lookback - timedelta(days=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    close_at = (now - timedelta(days=5)).strftime('%Y-%m-%dT%H:%M:%SZ')

    try_add_open_or_closed_pr(miner_eval, _pr_node(1, stale, close_at), PRState.CLOSED.value, lookback)

    assert miner_eval.total_closed_prs == 0
    assert miner_eval.total_prs == 0
    assert miner_eval.total_open_prs == 0
    assert miner_eval.total_merged_prs == 0


@patch('gittensor.utils.github_api_tools.bt.logging')
def test_fresh_closed_pr_still_goes_to_scored_bucket(_):
    """Regression: the drop-path must not accidentally capture fresh PRs."""
    miner_eval = MinerEvaluation(uid=74, hotkey='hk', github_id='1', github_pat='fake')
    now = datetime.now(timezone.utc)
    lookback = now - timedelta(days=35)
    fresh = (now - timedelta(days=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    close_at = (now - timedelta(days=5)).strftime('%Y-%m-%dT%H:%M:%SZ')

    try_add_open_or_closed_pr(miner_eval, _pr_node(1, fresh, close_at), PRState.CLOSED.value, lookback)

    assert len(miner_eval.closed_pull_requests) == 1
    assert len(miner_eval.stale_closed_pull_requests) == 0
    assert miner_eval.total_closed_prs == 1


@patch('gittensor.utils.github_api_tools.bt.logging')
def test_mixed_fresh_and_stale_closed_prs(_):
    miner_eval = MinerEvaluation(uid=74, hotkey='hk', github_id='1', github_pat='fake')
    now = datetime.now(timezone.utc)
    lookback = now - timedelta(days=35)
    fresh = (now - timedelta(days=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    stale = (lookback - timedelta(days=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    close_at = (now - timedelta(days=5)).strftime('%Y-%m-%dT%H:%M:%SZ')

    try_add_open_or_closed_pr(miner_eval, _pr_node(10, fresh, close_at), PRState.CLOSED.value, lookback)
    try_add_open_or_closed_pr(miner_eval, _pr_node(11, stale, close_at), PRState.CLOSED.value, lookback)

    assert len(miner_eval.closed_pull_requests) == 1
    assert len(miner_eval.stale_closed_pull_requests) == 1
    assert miner_eval.total_closed_prs == 1  # storage-only stale PRs do not inflate totals


@patch('gittensor.utils.github_api_tools.bt.logging')
def test_stale_closed_storage_bucket_does_not_inflate_any_totals(_):
    miner_eval = MinerEvaluation(uid=74, hotkey='hk', github_id='1', github_pat='fake')
    now = datetime.now(timezone.utc)
    lookback = now - timedelta(days=35)
    stale = (lookback - timedelta(days=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    close_at = (now - timedelta(days=5)).strftime('%Y-%m-%dT%H:%M:%SZ')

    for i in range(3):
        try_add_open_or_closed_pr(miner_eval, _pr_node(100 + i, stale, close_at), PRState.CLOSED.value, lookback)

    assert miner_eval.total_merged_prs == 0
    assert miner_eval.total_open_prs == 0
    assert miner_eval.total_closed_prs == 0
    assert miner_eval.total_prs == 0
    assert len(miner_eval.stale_closed_pull_requests) == 3
