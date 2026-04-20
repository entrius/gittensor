from gittensor.classes import PullRequest


def test_pull_request_handles_deleted_label_event():
    pr_data = {
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

    pr = PullRequest.from_graphql_response(pr_data, uid=1, hotkey='5Hotkey', github_id='123')

    assert pr.label is None
    assert pr.author_login == 'alice'
