#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Advanced tests for github_api_tools module covering untested functions.

Run with: python -m pytest tests/utils/test_github_api_advanced.py -v
"""

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set
from unittest.mock import MagicMock, Mock, call, patch

import pytest

github_api_tools = pytest.importorskip(
    'gittensor.utils.github_api_tools',
    reason='Requires gittensor package with all dependencies',
)

branch_matches_pattern = github_api_tools.branch_matches_pattern
execute_graphql_query = github_api_tools.execute_graphql_query
should_skip_merged_pr = github_api_tools.should_skip_merged_pr
try_add_open_or_closed_pr = github_api_tools.try_add_open_or_closed_pr
get_github_account_age_days = github_api_tools.get_github_account_age_days
fetch_file_contents_batch = github_api_tools.fetch_file_contents_batch
fetch_file_contents_with_base = github_api_tools.fetch_file_contents_with_base
FileContentPair = github_api_tools.FileContentPair
load_miners_prs = github_api_tools.load_miners_prs

from gittensor.classes import FileChange, MinerEvaluation, PRState
from gittensor.constants import MAINTAINER_ASSOCIATIONS, MAX_FILE_SIZE_BYTES
from gittensor.validator.utils.load_weights import RepositoryConfig


# ============================================================================
# Helpers
# ============================================================================

def _make_repo_config(
    weight: float = 1.0,
    inactive_at: Optional[str] = None,
    additional_acceptable_branches: Optional[List[str]] = None,
) -> RepositoryConfig:
    return RepositoryConfig(
        weight=weight,
        inactive_at=inactive_at,
        additional_acceptable_branches=additional_acceptable_branches,
    )


def _make_pr_raw(
    number: int = 1,
    state: str = 'MERGED',
    mergedAt: str = '2026-01-15T12:00:00Z',
    createdAt: str = '2026-01-10T12:00:00Z',
    closedAt: Optional[str] = None,
    author_login: str = 'contributor',
    mergedBy_login: str = 'maintainer',
    authorAssociation: str = 'NONE',
    baseRefName: str = 'main',
    headRefName: str = 'feature-branch',
    default_branch: str = 'main',
    repo_owner: str = 'owner',
    repo_name: str = 'repo',
    head_repo_owner: Optional[str] = None,
    head_repo_name: Optional[str] = None,
    reviews: Optional[list] = None,
) -> Dict:
    pr = {
        'number': number,
        'state': state,
        'mergedAt': mergedAt,
        'createdAt': createdAt,
        'closedAt': closedAt,
        'title': f'PR #{number}',
        'additions': 10,
        'deletions': 5,
        'bodyText': 'Test PR',
        'lastEditedAt': None,
        'commits': {'totalCount': 1},
        'author': {'login': author_login},
        'authorAssociation': authorAssociation,
        'mergedBy': {'login': mergedBy_login} if mergedBy_login else None,
        'baseRefName': baseRefName,
        'baseRefOid': 'abc123',
        'headRefName': headRefName,
        'headRefOid': 'def456',
        'repository': {
            'name': repo_name,
            'owner': {'login': repo_owner},
            'defaultBranchRef': {'name': default_branch} if default_branch else None,
        },
        'headRepository': {
            'name': head_repo_name or repo_name,
            'owner': {'login': head_repo_owner or 'fork-owner'},
        },
        'closingIssuesReferences': {'nodes': []},
        'reviews': {'nodes': reviews or []},
    }
    return pr


LOOKBACK = datetime.now(timezone.utc) - timedelta(days=90)


# ============================================================================
# branch_matches_pattern
# ============================================================================

class TestBranchMatchesPattern:
    def test_exact_match(self):
        assert branch_matches_pattern('main', ['main']) is True

    def test_no_match(self):
        assert branch_matches_pattern('develop', ['main', 'release']) is False

    def test_wildcard_suffix(self):
        assert branch_matches_pattern('3.0-dev', ['*-dev']) is True

    def test_wildcard_prefix(self):
        assert branch_matches_pattern('release-v2', ['release-*']) is True

    def test_star_matches_all(self):
        assert branch_matches_pattern('anything', ['*']) is True

    def test_empty_patterns(self):
        assert branch_matches_pattern('main', []) is False

    def test_multiple_patterns(self):
        assert branch_matches_pattern('staging', ['main', 'staging', 'develop']) is True

    def test_question_mark_wildcard(self):
        assert branch_matches_pattern('v1', ['v?']) is True
        assert branch_matches_pattern('v12', ['v?']) is False


# ============================================================================
# execute_graphql_query
# ============================================================================

class TestExecuteGraphqlQuery:
    @patch('gittensor.utils.github_api_tools.requests.post')
    def test_success_first_try(self, mock_post):
        resp = Mock(status_code=200)
        resp.json.return_value = {'data': {'viewer': {'login': 'test'}}}
        mock_post.return_value = resp

        result = execute_graphql_query('query {}', {}, 'token')
        assert result == {'data': {'viewer': {'login': 'test'}}}
        assert mock_post.call_count == 1

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_retry_then_success(self, mock_log, mock_sleep, mock_post):
        fail = Mock(status_code=502, text='Bad Gateway')
        ok = Mock(status_code=200)
        ok.json.return_value = {'data': {}}
        mock_post.side_effect = [fail, fail, ok]

        result = execute_graphql_query('q', {}, 'tok', max_attempts=5)
        assert result == {'data': {}}
        assert mock_post.call_count == 3
        # Backoff: 5, 10
        mock_sleep.assert_has_calls([call(5), call(10)])

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_all_attempts_fail(self, mock_log, mock_sleep, mock_post):
        fail = Mock(status_code=500, text='err')
        mock_post.return_value = fail

        result = execute_graphql_query('q', {}, 'tok', max_attempts=3)
        assert result is None
        assert mock_post.call_count == 3
        mock_log.error.assert_called()

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_request_exception_retry(self, mock_log, mock_sleep, mock_post):
        import requests
        ok = Mock(status_code=200)
        ok.json.return_value = {'data': {}}
        mock_post.side_effect = [
            requests.exceptions.Timeout('timeout'),
            ok,
        ]

        result = execute_graphql_query('q', {}, 'tok', max_attempts=3)
        assert result == {'data': {}}

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_all_request_exceptions_fail(self, mock_log, mock_sleep, mock_post):
        import requests
        mock_post.side_effect = requests.exceptions.ConnectionError('nope')

        result = execute_graphql_query('q', {}, 'tok', max_attempts=2)
        assert result is None

    @patch('gittensor.utils.github_api_tools.requests.post')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    @patch('gittensor.utils.github_api_tools.bt.logging')
    def test_backoff_capped_at_30(self, mock_log, mock_sleep, mock_post):
        fail = Mock(status_code=500, text='err')
        mock_post.return_value = fail

        execute_graphql_query('q', {}, 'tok', max_attempts=6)
        # Delays: min(5*1,30)=5, min(5*2,30)=10, min(5*4,30)=20, min(5*8,30)=30, min(5*16,30)=30
        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays == [5, 10, 20, 30, 30]


# ============================================================================
# should_skip_merged_pr
# ============================================================================

class TestShouldSkipMergedPr:
    def test_valid_pr_passes(self):
        pr = _make_pr_raw()
        skip, reason = should_skip_merged_pr(pr, 'owner/repo', _make_repo_config(), LOOKBACK)
        assert skip is False
        assert reason is None

    def test_missing_mergedAt(self):
        pr = _make_pr_raw(mergedAt=None)
        skip, reason = should_skip_merged_pr(pr, 'owner/repo', _make_repo_config(), LOOKBACK)
        assert skip is True
        assert 'missing a mergedAt' in reason

    def test_merged_before_lookback(self):
        old_date = (LOOKBACK - timedelta(days=10)).isoformat() + 'Z'
        pr = _make_pr_raw(mergedAt=old_date)
        skip, reason = should_skip_merged_pr(pr, 'owner/repo', _make_repo_config(), LOOKBACK)
        assert skip is True
        assert 'lookback' in reason.lower()

    def test_maintainer_author_skipped(self):
        for assoc in MAINTAINER_ASSOCIATIONS:
            pr = _make_pr_raw(authorAssociation=assoc)
            skip, _ = should_skip_merged_pr(pr, 'owner/repo', _make_repo_config(), LOOKBACK)
            assert skip is True

    def test_self_merge_no_approval_skipped(self):
        pr = _make_pr_raw(author_login='alice', mergedBy_login='alice')
        skip, reason = should_skip_merged_pr(pr, 'owner/repo', _make_repo_config(), LOOKBACK)
        assert skip is True
        assert 'self-merged' in reason

    def test_self_merge_with_external_approval_passes(self):
        pr = _make_pr_raw(
            author_login='alice',
            mergedBy_login='alice',
            reviews=[{'author': {'login': 'bob'}}],
        )
        skip, _ = should_skip_merged_pr(pr, 'owner/repo', _make_repo_config(), LOOKBACK)
        assert skip is False

    def test_wrong_base_branch_skipped(self):
        pr = _make_pr_raw(baseRefName='feature-xyz')
        skip, reason = should_skip_merged_pr(pr, 'owner/repo', _make_repo_config(), LOOKBACK)
        assert skip is True
        assert 'not default branch' in reason

    def test_additional_acceptable_branch(self):
        pr = _make_pr_raw(baseRefName='develop')
        cfg = _make_repo_config(additional_acceptable_branches=['develop'])
        skip, _ = should_skip_merged_pr(pr, 'owner/repo', cfg, LOOKBACK)
        assert skip is False

    def test_wildcard_acceptable_branch(self):
        pr = _make_pr_raw(baseRefName='3.0-dev')
        cfg = _make_repo_config(additional_acceptable_branches=['*-dev'])
        skip, _ = should_skip_merged_pr(pr, 'owner/repo', cfg, LOOKBACK)
        assert skip is False

    def test_internal_pr_between_acceptable_branches_skipped(self):
        """PR from staging->main where both are acceptable should be skipped."""
        pr = _make_pr_raw(
            baseRefName='main',
            headRefName='develop',
            head_repo_owner='owner',
            head_repo_name='repo',
        )
        # headRepository matches repo, so it's internal
        pr['headRepository'] = {'name': 'repo', 'owner': {'login': 'owner'}}
        cfg = _make_repo_config(additional_acceptable_branches=['develop'])
        skip, reason = should_skip_merged_pr(pr, 'owner/repo', cfg, LOOKBACK)
        assert skip is True
        assert 'acceptable branch' in reason

    def test_fork_pr_from_acceptable_branch_name_passes(self):
        """Fork PRs should NOT be skipped even if headRef matches acceptable branch."""
        pr = _make_pr_raw(
            baseRefName='main',
            headRefName='main',  # fork's branch happens to be named 'main'
            head_repo_owner='fork-owner',
        )
        skip, _ = should_skip_merged_pr(pr, 'owner/repo', _make_repo_config(), LOOKBACK)
        assert skip is False

    def test_no_default_branch_ref_falls_back_to_main(self):
        pr = _make_pr_raw(default_branch=None, baseRefName='main')
        skip, _ = should_skip_merged_pr(pr, 'owner/repo', _make_repo_config(), LOOKBACK)
        assert skip is False

    def test_mergedBy_none_not_self_merge(self):
        pr = _make_pr_raw(mergedBy_login=None)
        skip, _ = should_skip_merged_pr(pr, 'owner/repo', _make_repo_config(), LOOKBACK)
        assert skip is False


# ============================================================================
# try_add_open_or_closed_pr
# ============================================================================

class TestTryAddOpenOrClosedPr:
    def _make_miner_eval(self):
        return MinerEvaluation(uid=0, hotkey='hk')

    def test_open_pr_added(self):
        me = self._make_miner_eval()
        pr = _make_pr_raw(state='OPEN', authorAssociation='NONE')
        try_add_open_or_closed_pr(me, pr, PRState.OPEN.value, LOOKBACK)
        assert len(me.open_pull_requests) == 1

    def test_closed_pr_within_lookback_added(self):
        me = self._make_miner_eval()
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat() + 'Z'
        pr = _make_pr_raw(state='CLOSED', closedAt=recent, authorAssociation='NONE')
        try_add_open_or_closed_pr(me, pr, PRState.CLOSED.value, LOOKBACK)
        assert len(me.closed_pull_requests) == 1

    def test_closed_pr_before_lookback_not_added(self):
        me = self._make_miner_eval()
        old = (LOOKBACK - timedelta(days=10)).isoformat() + 'Z'
        pr = _make_pr_raw(state='CLOSED', closedAt=old, authorAssociation='NONE')
        try_add_open_or_closed_pr(me, pr, PRState.CLOSED.value, LOOKBACK)
        assert len(me.closed_pull_requests) == 0

    def test_closed_pr_missing_closedAt(self):
        me = self._make_miner_eval()
        pr = _make_pr_raw(state='CLOSED', closedAt=None, authorAssociation='NONE')
        try_add_open_or_closed_pr(me, pr, PRState.CLOSED.value, LOOKBACK)
        assert len(me.closed_pull_requests) == 0

    def test_maintainer_pr_ignored(self):
        me = self._make_miner_eval()
        for assoc in MAINTAINER_ASSOCIATIONS:
            pr = _make_pr_raw(state='OPEN', authorAssociation=assoc)
            try_add_open_or_closed_pr(me, pr, PRState.OPEN.value, LOOKBACK)
        assert len(me.open_pull_requests) == 0


# ============================================================================
# get_github_account_age_days
# ============================================================================

class TestGetGithubAccountAgeDays:
    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_valid_account(self, mock_get, clear_github_cache):
        resp = Mock(status_code=200)
        resp.json.return_value = {'id': 1, 'created_at': '2020-06-15T00:00:00Z'}
        mock_get.return_value = resp

        age = get_github_account_age_days('tok_age1')
        expected_min = (datetime.now(timezone.utc) - datetime(2020, 6, 15, tzinfo=timezone.utc)).days
        assert abs(age - expected_min) <= 1

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_missing_created_at(self, mock_get, clear_github_cache):
        resp = Mock(status_code=200)
        resp.json.return_value = {'id': 2}
        mock_get.return_value = resp
        assert get_github_account_age_days('tok_age2') is None

    @patch('gittensor.utils.github_api_tools.requests.get')
    def test_empty_token(self, mock_get, clear_github_cache):
        assert get_github_account_age_days('') is None
        mock_get.assert_not_called()

    @patch('gittensor.utils.github_api_tools.requests.get')
    @patch('gittensor.utils.github_api_tools.time.sleep')
    def test_all_requests_fail(self, mock_sleep, mock_get, clear_github_cache):
        mock_get.side_effect = Exception('network')
        assert get_github_account_age_days('tok_age3') is None


# ============================================================================
# fetch_file_contents_batch
# ============================================================================

class TestFetchFileContentsBatch:
    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_empty_paths(self, mock_exec):
        result = fetch_file_contents_batch('o', 'r', 'sha', [], 'tok')
        assert result == {}
        mock_exec.assert_not_called()

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_normal_files(self, mock_exec):
        mock_exec.return_value = {
            'data': {
                'repository': {
                    'file0': {'text': 'hello', 'byteSize': 5, 'isBinary': False},
                    'file1': {'text': 'world', 'byteSize': 5, 'isBinary': False},
                }
            }
        }
        result = fetch_file_contents_batch('o', 'r', 'sha', ['a.py', 'b.py'], 'tok')
        assert result == {'a.py': 'hello', 'b.py': 'world'}

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_binary_file_returns_none(self, mock_exec):
        mock_exec.return_value = {
            'data': {
                'repository': {
                    'file0': {'text': None, 'byteSize': 100, 'isBinary': True},
                }
            }
        }
        result = fetch_file_contents_batch('o', 'r', 'sha', ['img.png'], 'tok')
        assert result == {'img.png': None}

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_oversized_file_returns_none(self, mock_exec):
        mock_exec.return_value = {
            'data': {
                'repository': {
                    'file0': {'text': 'big', 'byteSize': MAX_FILE_SIZE_BYTES + 1, 'isBinary': False},
                }
            }
        }
        result = fetch_file_contents_batch('o', 'r', 'sha', ['big.txt'], 'tok')
        assert result == {'big.txt': None}

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_deleted_file_returns_none(self, mock_exec):
        mock_exec.return_value = {
            'data': {'repository': {'file0': None}}
        }
        result = fetch_file_contents_batch('o', 'r', 'sha', ['gone.py'], 'tok')
        assert result == {'gone.py': None}

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_query_failure_returns_all_none(self, mock_exec):
        mock_exec.return_value = None
        result = fetch_file_contents_batch('o', 'r', 'sha', ['a.py', 'b.py'], 'tok')
        assert result == {'a.py': None, 'b.py': None}

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_graphql_errors_still_returns_data(self, mock_exec):
        mock_exec.return_value = {
            'errors': [{'message': 'partial'}],
            'data': {
                'repository': {
                    'file0': {'text': 'ok', 'byteSize': 2, 'isBinary': False},
                }
            }
        }
        result = fetch_file_contents_batch('o', 'r', 'sha', ['a.py'], 'tok')
        assert result == {'a.py': 'ok'}


# ============================================================================
# fetch_file_contents_with_base
# ============================================================================

class TestFetchFileContentsWithBase:
    def _fc(self, filename, status, previous_filename=None):
        return FileChange(
            pr_number=1,
            repository_full_name='o/r',
            filename=filename,
            changes=1,
            additions=1,
            deletions=0,
            status=status,
            previous_filename=previous_filename,
        )

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_empty_changes(self, mock_exec):
        assert fetch_file_contents_with_base('o', 'r', 'b', 'h', [], 'tok') == {}
        mock_exec.assert_not_called()

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_added_file(self, mock_exec):
        mock_exec.return_value = {
            'data': {
                'repository': {
                    'head0': {'text': 'new content', 'byteSize': 11, 'isBinary': False},
                }
            }
        }
        fc = self._fc('new.py', 'added')
        result = fetch_file_contents_with_base('o', 'r', 'b', 'h', [fc], 'tok')
        assert result['new.py'].old_content is None
        assert result['new.py'].new_content == 'new content'

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_removed_file(self, mock_exec):
        mock_exec.return_value = {
            'data': {
                'repository': {
                    'base0': {'text': 'old content', 'byteSize': 11, 'isBinary': False},
                }
            }
        }
        fc = self._fc('old.py', 'removed')
        result = fetch_file_contents_with_base('o', 'r', 'b', 'h', [fc], 'tok')
        assert result['old.py'].old_content == 'old content'
        assert result['old.py'].new_content is None

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_modified_file(self, mock_exec):
        mock_exec.return_value = {
            'data': {
                'repository': {
                    'base0': {'text': 'v1', 'byteSize': 2, 'isBinary': False},
                    'head0': {'text': 'v2', 'byteSize': 2, 'isBinary': False},
                }
            }
        }
        fc = self._fc('mod.py', 'modified')
        result = fetch_file_contents_with_base('o', 'r', 'b', 'h', [fc], 'tok')
        assert result['mod.py'].old_content == 'v1'
        assert result['mod.py'].new_content == 'v2'

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_renamed_file(self, mock_exec):
        mock_exec.return_value = {
            'data': {
                'repository': {
                    'base0': {'text': 'content', 'byteSize': 7, 'isBinary': False},
                    'head0': {'text': 'content', 'byteSize': 7, 'isBinary': False},
                }
            }
        }
        fc = self._fc('new_name.py', 'renamed', previous_filename='old_name.py')
        result = fetch_file_contents_with_base('o', 'r', 'b', 'h', [fc], 'tok')
        assert result['new_name.py'].old_content == 'content'
        assert result['new_name.py'].new_content == 'content'

    @patch('gittensor.utils.github_api_tools.execute_graphql_query')
    def test_query_failure(self, mock_exec):
        mock_exec.return_value = None
        fc = self._fc('f.py', 'modified')
        result = fetch_file_contents_with_base('o', 'r', 'b', 'h', [fc], 'tok')
        assert result['f.py'].old_content is None
        assert result['f.py'].new_content is None


# ============================================================================
# load_miners_prs (integration with mocks)
# ============================================================================

class TestLoadMinersPrs:
    def _make_eval(self):
        return MinerEvaluation(uid=0, hotkey='hk', github_id='12345', github_pat='tok')

    def _graphql_response(self, prs, has_next=False, cursor=None):
        resp = Mock(status_code=200)
        resp.json.return_value = {
            'data': {
                'node': {
                    'pullRequests': {
                        'pageInfo': {'hasNextPage': has_next, 'endCursor': cursor},
                        'nodes': prs,
                    }
                }
            }
        }
        return resp

    @patch('gittensor.utils.github_api_tools.get_github_graphql_query')
    def test_loads_merged_pr(self, mock_gql):
        me = self._make_eval()
        pr = _make_pr_raw(
            createdAt='2026-01-10T00:00:00Z',
            mergedAt='2026-01-15T00:00:00Z',
        )
        mock_gql.return_value = self._graphql_response([pr])
        repos = {'owner/repo': _make_repo_config()}

        load_miners_prs(me, repos)
        assert len(me.merged_pull_requests) == 1

    @patch('gittensor.utils.github_api_tools.get_github_graphql_query')
    def test_skips_ineligible_repo(self, mock_gql):
        me = self._make_eval()
        pr = _make_pr_raw(repo_owner='other', repo_name='unknown', createdAt='2026-01-10T00:00:00Z')
        mock_gql.return_value = self._graphql_response([pr])

        load_miners_prs(me, {'owner/repo': _make_repo_config()})
        assert len(me.merged_pull_requests) == 0

    @patch('gittensor.utils.github_api_tools.get_github_graphql_query')
    def test_skips_inactive_repo_pr(self, mock_gql):
        me = self._make_eval()
        pr = _make_pr_raw(createdAt='2026-02-01T00:00:00Z', mergedAt='2026-02-05T00:00:00Z')
        mock_gql.return_value = self._graphql_response([pr])
        repos = {'owner/repo': _make_repo_config(inactive_at='2026-01-01T00:00:00Z')}

        load_miners_prs(me, repos)
        assert len(me.merged_pull_requests) == 0

    @patch('gittensor.utils.github_api_tools.get_github_graphql_query')
    def test_open_pr_categorized(self, mock_gql):
        me = self._make_eval()
        pr = _make_pr_raw(state='OPEN', createdAt='2026-01-10T00:00:00Z')
        mock_gql.return_value = self._graphql_response([pr])
        repos = {'owner/repo': _make_repo_config()}

        load_miners_prs(me, repos)
        assert len(me.open_pull_requests) == 1
        assert len(me.merged_pull_requests) == 0

    @patch('gittensor.utils.github_api_tools.get_github_graphql_query')
    def test_no_response_breaks_loop(self, mock_gql):
        me = self._make_eval()
        mock_gql.return_value = None
        load_miners_prs(me, {})
        assert len(me.merged_pull_requests) == 0

    @patch('gittensor.utils.github_api_tools.get_github_graphql_query')
    def test_graphql_errors_breaks_loop(self, mock_gql):
        me = self._make_eval()
        resp = Mock(status_code=200)
        resp.json.return_value = {'errors': [{'message': 'bad'}]}
        mock_gql.return_value = resp

        load_miners_prs(me, {})
        assert len(me.merged_pull_requests) == 0

    @patch('gittensor.utils.github_api_tools.get_github_graphql_query')
    def test_stops_at_tier_incentive_start_date(self, mock_gql):
        me = self._make_eval()
        # PR created before the tier incentive start date
        pr = _make_pr_raw(createdAt='2020-01-01T00:00:00Z', mergedAt='2020-01-05T00:00:00Z')
        mock_gql.return_value = self._graphql_response([pr])
        repos = {'owner/repo': _make_repo_config()}

        load_miners_prs(me, repos)
        assert len(me.merged_pull_requests) == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
