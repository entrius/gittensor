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


def test_pull_request_parses_all_closing_issues_from_graphql_node_list():
    pr_data = {
        'number': 77,
        'repository': {'owner': {'login': 'entrius'}, 'name': 'gittensor'},
        'state': 'MERGED',
        'closingIssuesReferences': {
            'nodes': [
                {
                    'number': issue_number,
                    'title': f'Issue {issue_number}',
                    'state': 'CLOSED',
                    'stateReason': 'COMPLETED',
                    'createdAt': '2026-04-10T00:00:00Z',
                    'closedAt': '2026-04-11T00:00:00Z',
                    'updatedAt': '2026-04-11T00:00:00Z',
                    'author': {'login': 'reporter', 'databaseId': 123},
                    'authorAssociation': 'CONTRIBUTOR',
                    'userContentEdits': {'nodes': []},
                    'timelineItems': {'nodes': []},
                }
                for issue_number in [101, 102, 103, 104, 105]
            ]
        },
        'bodyText': 'Fix multiple issues',
        'lastEditedAt': None,
        'mergedAt': '2026-04-12T00:00:00Z',
        'timelineItems': {'nodes': []},
        'labels': {'nodes': []},
        'title': 'fix: close multiple issues',
        'author': {'login': 'alice'},
        'createdAt': '2026-04-09T00:00:00Z',
        'additions': 10,
        'deletions': 2,
        'commits': {'totalCount': 1},
        'headRefOid': 'abc123',
        'baseRefOid': 'def456',
        'changesRequestedReviews': {'nodes': []},
    }

    pr = PullRequest.from_graphql_response(pr_data, uid=1, hotkey='5Hotkey', github_id='123')

    assert pr.issues is not None
    assert [issue.number for issue in pr.issues] == [101, 102, 103, 104, 105]
