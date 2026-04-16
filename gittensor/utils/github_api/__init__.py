# Entrius 2025
"""GitHub REST/GraphQL helpers split by concern (REST, GraphQL, issue PR search, miner loader, file contents)."""

from gittensor.utils.github_api.file_contents import (
    FileContentPair,
    fetch_file_contents_batch,
    fetch_file_contents_with_base,
)
from gittensor.utils.github_api.graphql import (
    QUERY,
    GraphQLPageResult,
    execute_graphql_query,
    get_github_graphql_query,
)
from gittensor.utils.github_api.issue_pr_search import (
    _search_issue_referencing_prs_graphql,
    _search_issue_referencing_prs_rest,
    check_github_issue_closed,
    find_prs_for_issue,
    find_solver_from_cross_references,
    find_solver_from_timeline,
)
from gittensor.utils.github_api.miner_pr_loader import (
    load_miners_prs,
    should_skip_merged_pr,
    try_add_open_or_closed_pr,
)
from gittensor.utils.github_api.rest import (
    branch_matches_pattern,
    get_github_id,
    get_github_user,
    get_merge_base_sha,
    get_pull_request_file_changes,
    get_pull_request_maintainer_changes_requested_count,
    make_headers,
)

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
