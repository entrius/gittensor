from datetime import datetime, timezone

import pytest

from gittensor.validator.utils.load_weights import RepositoryConfig


github_api_tools = pytest.importorskip(
    'gittensor.utils.github_api_tools', reason='Requires gittensor package with all dependencies'
)

should_skip_merged_pr = github_api_tools.should_skip_merged_pr


def test_should_skip_merged_pr_allows_null_author_without_crashing():
    pr_raw = {
        'number': 42,
        'author': None,
        'authorAssociation': 'NONE',
        'mergedBy': {'login': 'maintainer'},
        'reviews': {'nodes': []},
        'repository': {'defaultBranchRef': {'name': 'main'}},
        'baseRefName': 'main',
        'headRefName': 'feature/null-author',
        'headRepository': {'owner': {'login': 'forker'}, 'name': 'gittensor'},
        'mergedAt': datetime.now(timezone.utc).isoformat(),
    }

    should_skip, reason = should_skip_merged_pr(
        pr_raw,
        repository_full_name='entrius/gittensor',
        repo_config=RepositoryConfig(weight=1.0),
        lookback_date_filter=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert should_skip is False
    assert reason is None
