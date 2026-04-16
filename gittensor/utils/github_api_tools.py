# Entrius 2025
"""Backward-compatible entrypoint for GitHub API utilities."""

from gittensor.utils.github_api import QUERY
from gittensor.utils.github_api import FileContentPair
from gittensor.utils.github_api import GraphQLPageResult
from gittensor.utils.github_api import _search_issue_referencing_prs_graphql
from gittensor.utils.github_api import _search_issue_referencing_prs_rest
from gittensor.utils.github_api import branch_matches_pattern
from gittensor.utils.github_api import check_github_issue_closed
from gittensor.utils.github_api import execute_graphql_query
from gittensor.utils.github_api import fetch_file_contents_batch
from gittensor.utils.github_api import fetch_file_contents_with_base
from gittensor.utils.github_api import find_prs_for_issue
from gittensor.utils.github_api import find_solver_from_cross_references
from gittensor.utils.github_api import find_solver_from_timeline
from gittensor.utils.github_api import get_github_graphql_query
from gittensor.utils.github_api import get_github_id
from gittensor.utils.github_api import get_github_user
from gittensor.utils.github_api import get_merge_base_sha
from gittensor.utils.github_api import get_pull_request_file_changes
from gittensor.utils.github_api import get_pull_request_maintainer_changes_requested_count
from gittensor.utils.github_api import load_miners_prs
from gittensor.utils.github_api import make_headers
from gittensor.utils.github_api import should_skip_merged_pr
from gittensor.utils.github_api import try_add_open_or_closed_pr

__all__ = [
    'QUERY',
    'GraphQLPageResult',
    'FileContentPair',
    '_search_issue_referencing_prs_graphql',
    '_search_issue_referencing_prs_rest',
    'branch_matches_pattern',
    'check_github_issue_closed',
    'execute_graphql_query',
    'fetch_file_contents_batch',
    'fetch_file_contents_with_base',
    'find_prs_for_issue',
    'find_solver_from_cross_references',
    'find_solver_from_timeline',
    'get_github_graphql_query',
    'get_github_id',
    'get_github_user',
    'get_merge_base_sha',
    'get_pull_request_file_changes',
    'get_pull_request_maintainer_changes_requested_count',
    'load_miners_prs',
    'make_headers',
    'should_skip_merged_pr',
    'try_add_open_or_closed_pr',
]
