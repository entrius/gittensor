from gittensor.classes import PullRequest


def _base_pr_data():
    return {
        'number': 42,
        'repository': {'owner': {'login': 'entrius'}, 'name': 'gittensor'},
        'state': 'OPEN',
        'closingIssuesReferences': {'nodes': []},
        'bodyText': 'Fix bug',
        'lastEditedAt': None,
        'mergedAt': None,
        'timelineItems': {'nodes': [{'label': None}]},
        'title': 'fix: guard deleted label events',
        'author': {'login': 'alice'},
        'createdAt': '2026-04-18T00:00:00Z',
        'additions': 3,
        'deletions': 1,
        'commits': {'totalCount': 1},
        'headRefOid': 'abc123',
        'baseRefOid': 'def456',
    }


def test_pull_request_handles_deleted_label_event():
    pr = PullRequest.from_graphql_response(_base_pr_data(), uid=1, hotkey='5Hotkey', github_id='123')

    assert pr.label is None
    assert pr.author_login == 'alice'


def test_pull_request_handles_null_author():
    pr_data = _base_pr_data()
    pr_data['author'] = None

    pr = PullRequest.from_graphql_response(pr_data, uid=1, hotkey='5Hotkey', github_id='123')

    assert pr.author_login == 'ghost'
