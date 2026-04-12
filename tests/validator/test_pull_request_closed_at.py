# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for PullRequest.closed_at field parsing from GraphQL responses."""

from gittensor.classes import PullRequest

_BASE_PR_DATA = {
    'number': 1,
    'title': 'test',
    'author': {'login': 'user'},
    'createdAt': '2025-01-01T00:00:00Z',
    'additions': 10,
    'deletions': 5,
    'repository': {'owner': {'login': 'owner'}, 'name': 'repo'},
    'closingIssuesReferences': {'nodes': []},
}


def _parse(state: str, closed_at=None, merged_at=None) -> PullRequest:
    data = {**_BASE_PR_DATA, 'state': state, 'closedAt': closed_at, 'mergedAt': merged_at}
    if merged_at:
        data['mergedBy'] = {'login': 'maintainer'}
        data['commits'] = {'totalCount': 1}
    return PullRequest.from_graphql_response(data, uid=1, hotkey='k', github_id='1')


def test_closed_pr_has_closed_at():
    pr = _parse('CLOSED', closed_at='2025-01-15T00:00:00Z')
    assert pr.closed_at is not None
    assert pr.merged_at is None


def test_merged_pr_has_closed_at():
    pr = _parse('MERGED', closed_at='2025-01-20T00:00:00Z', merged_at='2025-01-20T00:00:00Z')
    assert pr.closed_at is not None
    assert pr.merged_at is not None


def test_open_pr_has_no_closed_at():
    pr = _parse('OPEN')
    assert pr.closed_at is None
