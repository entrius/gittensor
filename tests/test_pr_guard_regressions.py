from datetime import datetime, timedelta, timezone

from gittensor.classes import PullRequest
from gittensor.utils.github_api_tools import should_skip_merged_pr
from gittensor.validator.utils.load_weights import RepositoryConfig


def test_pull_request_handles_null_author():
    pr_data = {
        'number': 43,
        'repository': {'owner': {'login': 'entrius'}, 'name': 'gittensor'},
        'state': 'MERGED',
        'closingIssuesReferences': {'nodes': []},
        'bodyText': 'Fix bug',
        'lastEditedAt': None,
        'mergedAt': '2026-04-18T00:00:00Z',
        'timelineItems': {'nodes': []},
        'title': 'fix: guard null authors',
        'author': None,
        'createdAt': '2026-04-17T23:00:00Z',
        'additions': 3,
        'deletions': 1,
        'commits': {'totalCount': 1},
        'mergedBy': {'login': 'maintainer'},
        'headRefOid': 'abc123',
        'baseRefOid': 'def456',
    }

    pr = PullRequest.from_graphql_response(pr_data, uid=1, hotkey='5Hotkey', github_id='123')

    assert pr.author_login is None
    assert pr.merged_by_login == 'maintainer'


def test_should_skip_merged_pr_handles_null_author():
    pr_raw = {
        'number': 44,
        'mergedAt': '2026-04-18T00:00:00Z',
        'authorAssociation': 'CONTRIBUTOR',
        'author': None,
        'mergedBy': {'login': 'maintainer'},
        'reviews': {'nodes': []},
        'repository': {'defaultBranchRef': {'name': 'main'}},
        'baseRefName': 'main',
        'headRefName': 'feature-branch',
        'headRepository': {'owner': {'login': 'forkuser'}, 'name': 'gittensor'},
    }

    should_skip, reason = should_skip_merged_pr(
        pr_raw,
        'entrius/gittensor',
        RepositoryConfig(weight=1.0),
        datetime.now(timezone.utc) - timedelta(days=30),
    )

    assert should_skip is False
    assert reason is None
