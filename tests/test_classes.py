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


def test_pull_request_handles_null_merged_by():
    # GitHub returns mergedBy=null for bot merges or deleted merger accounts.
    # The previous parser used pr_data.get('mergedBy', {}).get('login') which
    # returns None (not {}) when the key is present with value None, then crashes
    # with AttributeError on the second .get().
    pr_data = _base_pr_data()
    pr_data['state'] = 'MERGED'
    pr_data['mergedAt'] = '2026-04-18T12:00:00Z'
    pr_data['mergedBy'] = None
    pr_data['changesRequestedReviews'] = {'nodes': []}

    pr = PullRequest.from_graphql_response(pr_data, uid=1, hotkey='5Hotkey', github_id='123')

    assert pr.merged_by_login is None
    assert pr.author_login == 'alice'
