"""Scoped open issue count — filters User.issues to tracked repos only.

Fixes the bug where the spam multiplier gated on the global User.issues.totalCount
(including personal projects and non-tracked upstreams), silently zeroing
miner scores.
"""

from unittest.mock import patch

from gittensor.utils.github_api_tools import count_tracked_open_issues

TRACKED = {'owner/repo-a', 'owner/repo-b'}


def _page(nodes, has_next=False, cursor=None):
    return {
        'data': {
            'node': {
                'issues': {
                    'pageInfo': {'hasNextPage': has_next, 'endCursor': cursor},
                    'nodes': nodes,
                }
            }
        }
    }


def _issue(repo):
    return {'repository': {'nameWithOwner': repo}}


def test_counts_only_tracked_repos():
    response = _page(
        [
            _issue('owner/repo-a'),
            _issue('owner/repo-a'),
            _issue('someone/personal-sandbox'),  # not tracked → must be excluded
            _issue('owner/repo-b'),
            _issue('other/upstream'),  # not tracked → must be excluded
        ]
    )
    with patch('gittensor.utils.github_api_tools.execute_graphql_query', return_value=response):
        assert count_tracked_open_issues('tok', 'node-id', TRACKED) == 3


def test_empty_tracked_set_returns_zero():
    # Short-circuit without hitting the API.
    with patch('gittensor.utils.github_api_tools.execute_graphql_query') as mock:
        assert count_tracked_open_issues('tok', 'node-id', set()) == 0
        mock.assert_not_called()


def test_missing_token_returns_zero():
    with patch('gittensor.utils.github_api_tools.execute_graphql_query') as mock:
        assert count_tracked_open_issues('', 'node-id', TRACKED) == 0
        mock.assert_not_called()


def test_graphql_failure_returns_none():
    """None = signal to caller; caller defaults to 0 (no penalty) on failure.
    Safer direction — a broken fetch must not silently demote a miner."""
    with patch('gittensor.utils.github_api_tools.execute_graphql_query', return_value=None):
        assert count_tracked_open_issues('tok', 'node-id', TRACKED) is None


def test_graphql_error_block_returns_none():
    with patch(
        'gittensor.utils.github_api_tools.execute_graphql_query',
        return_value={'errors': [{'message': 'boom'}]},
    ):
        assert count_tracked_open_issues('tok', 'node-id', TRACKED) is None


def test_pagination_accumulates_across_pages():
    pages = [
        _page([_issue('owner/repo-a'), _issue('other/upstream')], has_next=True, cursor='c1'),
        _page([_issue('owner/repo-b'), _issue('owner/repo-a')], has_next=False),
    ]
    with patch('gittensor.utils.github_api_tools.execute_graphql_query', side_effect=pages):
        assert count_tracked_open_issues('tok', 'node-id', TRACKED) == 3


def test_hits_max_pages_cap_returns_partial():
    """Pathological account with huge open-issue history — return partial rather than crash."""
    cap = 2
    pages = [_page([_issue('owner/repo-a')], has_next=True, cursor=f'c{i}') for i in range(cap)]
    with patch('gittensor.utils.github_api_tools.execute_graphql_query', side_effect=pages):
        result = count_tracked_open_issues('tok', 'node-id', TRACKED, max_pages=cap)
        assert result == cap  # one hit per page


def test_case_insensitive_repo_match():
    """master_repositories is lowercased by load_master_repo_weights, but
    GitHub returns nameWithOwner in original case. Both sides must normalize."""
    response = _page(
        [
            _issue('Owner/Repo-A'),  # mixed case from GitHub
            _issue('OWNER/REPO-B'),  # all caps
            _issue('Other/Upstream'),  # not tracked
        ]
    )
    with patch('gittensor.utils.github_api_tools.execute_graphql_query', return_value=response):
        assert count_tracked_open_issues('tok', 'node-id', TRACKED) == 2


def test_null_node_does_not_crash():
    """data.node can be explicitly null (deleted account, wrong id) — must not
    raise AttributeError on None.get('issues')."""
    response = {'data': {'node': None}}
    with patch('gittensor.utils.github_api_tools.execute_graphql_query', return_value=response):
        assert count_tracked_open_issues('tok', 'node-id', TRACKED) == 0
