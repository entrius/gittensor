# Entrius 2025
import base64
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import bittensor as bt

from gittensor.classes import MinerEvaluation, PRState
from gittensor.constants import MAINTAINER_ASSOCIATIONS, PR_LOOKBACK_DAYS
from gittensor.utils.github_api.rest import branch_matches_pattern
from gittensor.utils.github_iso_time import parse_github_utc_iso
from gittensor.utils.utils import parse_repo_name
from gittensor.validator.utils.load_weights import RepositoryConfig

from . import graphql as gh_graphql


def try_add_open_or_closed_pr(
    miner_eval: MinerEvaluation,
    pr_raw: Dict,
    pr_state: str,
    lookback_date_filter: datetime,
) -> None:
    """
    Attempts to add an OPEN or CLOSED PR to miner_eval if eligible.

    Args:
        miner_eval: The MinerEvaluation to add the PR to
        pr_raw: Raw PR data from GraphQL
        pr_state: GitHub PR state (OPEN, CLOSED, MERGED)
        lookback_date_filter: Date filter for lookback period
    """
    # Ignore all maintainer contributions
    if not os.environ.get('DEV_MODE') and pr_raw.get('authorAssociation') in MAINTAINER_ASSOCIATIONS:
        return

    if pr_state == PRState.OPEN.value:
        miner_eval.add_open_pull_request(pr_raw)

    if pr_state == PRState.CLOSED.value:
        closed_at = pr_raw.get('closedAt')
        if not closed_at:
            bt.logging.warning(f'PR #{pr_raw["number"]} is CLOSED but missing closedAt timestamp.')
            return

        created_at = pr_raw.get('createdAt')
        if not created_at:
            bt.logging.warning(f'PR #{pr_raw["number"]} is CLOSED but missing createdAt timestamp.')
            return

        closed_dt = parse_github_utc_iso(closed_at)
        created_dt = parse_github_utc_iso(created_at)

        # Ignore stale PRs that were created before the scoring lookback window.
        # This allows users to close old PRs without receiving a fresh credibility penalty.
        if created_dt < lookback_date_filter:
            return

        if closed_dt >= lookback_date_filter:
            miner_eval.add_closed_pull_request(pr_raw)


def should_skip_merged_pr(
    pr_raw: Dict,
    repository_full_name: str,
    repo_config: RepositoryConfig,
    lookback_date_filter: datetime,
) -> tuple[bool, Optional[str]]:
    """
    Validate a merged PR against all eligibility criteria.

    Args:
        pr_raw (Dict): Raw PR data from GraphQL
        repository_full_name (str): Full repository name (owner/repo)
        repo_config (RepositoryConfig): Repository configuration
        lookback_date_filter (datetime): Date filter for lookback period

    Returns:
        tuple[bool, Optional[str]]: (should_skip, skip_reason) - True if PR should be skipped with reason
    """

    if not pr_raw['mergedAt']:
        return (True, f'PR #{pr_raw["number"]} is MERGED, but missing a mergedAt timestamp. Skipping...')

    merged_dt = parse_github_utc_iso(pr_raw['mergedAt'])

    # Filter by lookback window
    if merged_dt < lookback_date_filter:
        return (
            True,
            f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - merged before {PR_LOOKBACK_DAYS}-day lookback window',
        )

    # Skip if PR author is a maintainer
    author_association = pr_raw.get('authorAssociation')
    if not os.environ.get('DEV_MODE') and author_association in MAINTAINER_ASSOCIATIONS:
        return (
            True,
            f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - author is {author_association} (has direct merge capabilities)',
        )

    # Skip if PR was merged by the same person who created it (self-merge) AND there's no approvals from a differing party
    if pr_raw['mergedBy'] and pr_raw['author']['login'] == pr_raw['mergedBy']['login']:
        # Check if there are any approvals from users other than the author
        reviews = pr_raw.get('reviews', {}).get('nodes', [])
        has_external_approval = any(
            review.get('author') and review['author']['login'] != pr_raw['author']['login'] for review in reviews
        )

        if not has_external_approval:
            return (True, f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - self-merged, no approval')

    # Skip if PR was not merged to an acceptable branch (default or additional)
    default_branch = (
        pr_raw['repository']['defaultBranchRef']['name'] if pr_raw['repository']['defaultBranchRef'] else 'main'
    )
    base_ref = pr_raw['baseRefName']
    head_ref = pr_raw.get('headRefName', '')  # Source branch (where PR is coming FROM)
    additional_branches = repo_config.additional_acceptable_branches or []
    acceptable_branches = [default_branch] + additional_branches

    # Skip if the source branch (headRef) is also an acceptable branch
    # This prevents PRs like "staging -> main" or "develop -> staging" where both are acceptable branches
    # This check ONLY applies to internal PRs (same repository), as fork branch names are arbitrary.
    # Supports wildcard patterns (e.g., '*-dev' matches '3.0-dev', '3.1-dev', etc.)
    head_repo = pr_raw.get('headRepository')
    if head_repo and parse_repo_name(head_repo) == repository_full_name:
        if branch_matches_pattern(head_ref, acceptable_branches):
            return (
                True,
                f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - '
                f"source branch '{head_ref}' is an acceptable branch (merging between acceptable branches not allowed)",
            )

    # Check if merged to an acceptable branch (default or additional)
    # Supports wildcard patterns (e.g., '*-dev' matches '3.0-dev', '3.1-dev', etc.)
    if not branch_matches_pattern(base_ref, acceptable_branches):
        return (
            True,
            f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - '
            f"merged to '{base_ref}' (not default branch '{default_branch}' or additional acceptable branches)",
        )

    # All checks passed
    return (False, None)


def load_miners_prs(
    miner_eval: MinerEvaluation, master_repositories: Dict[str, RepositoryConfig], max_prs: int = 1000
) -> None:
    """
    Fetches user PRs via GraphQL API and categorize them by state.
    Populates the provided miner_eval instance with fetched PR data.

    Args:
        miner_eval: The MinerEvaluation object containing github details + more
        master_repositories: Repository metadata (name -> RepositoryConfig)
        max_prs: Maximum merged PRs to fetch
    """
    bt.logging.info('*****Fetching PRs*****')

    if not miner_eval.github_pat:
        bt.logging.warning(f'UID {miner_eval.uid} has no github_pat, skipping PR fetch')
        return

    lookback_date_filter = datetime.now(timezone.utc) - timedelta(days=PR_LOOKBACK_DAYS)
    global_user_id = base64.b64encode(f'04:User{miner_eval.github_id}'.encode()).decode()

    cursor = None
    current_page_size: Optional[int] = None  # None = let get_github_graphql_query choose default

    try:
        while len(miner_eval.merged_pull_requests) < max_prs:
            result = gh_graphql.get_github_graphql_query(
                miner_eval.github_pat,
                global_user_id,
                len(miner_eval.merged_pull_requests),
                max_prs,
                cursor,
                page_size=current_page_size,
            )

            # Carry reduced page size forward for subsequent pages
            current_page_size = result.page_size

            if not result.response:
                bt.logging.warning('No response from github, breaking fetch loop...')
                break

            data: Dict = result.response.json()

            # Resource limit errors are already handled in get_github_graphql_query; break on others
            if 'errors' in data:
                non_resource_errors = [e for e in data['errors'] if e.get('type') != 'RESOURCE_LIMITS_EXCEEDED']
                if non_resource_errors:
                    bt.logging.error(f'GraphQL errors: {non_resource_errors}')
                    break

            user_data: Dict = data.get('data', {}).get('node')
            if not user_data:
                bt.logging.warning('User not found or no pull requests')
                break

            # Extract open issue count from first page (User-level field, not paginated)
            if cursor is None:
                miner_eval.total_open_issues = user_data.get('issues', {}).get('totalCount', 0)

            pr_data: Dict = user_data.get('pullRequests', {})
            prs: List = pr_data.get('nodes', [])
            page_info: Dict = pr_data.get('pageInfo', {})

            for pr_raw in prs:
                try:
                    repository_full_name = parse_repo_name(pr_raw['repository'])
                    pr_state = pr_raw['state']

                    if repository_full_name not in master_repositories:
                        bt.logging.info(f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - ineligible repo')
                        continue

                    repo_config = master_repositories[repository_full_name]

                    # Check if repo is inactive
                    if repo_config.inactive_at is not None:
                        inactive_dt = parse_github_utc_iso(repo_config.inactive_at)
                        pr_creation_time = parse_github_utc_iso(pr_raw['createdAt'])
                        # Skip PR if it was created after the repo became inactive
                        if pr_creation_time >= inactive_dt:
                            bt.logging.info(
                                f'Skipping PR #{pr_raw["number"]} in {repository_full_name} - PR was created after repo became inactive (created: {pr_creation_time.isoformat()}, inactive: {inactive_dt.isoformat()})'
                            )
                            continue

                    if pr_state in (PRState.OPEN.value, PRState.CLOSED.value):
                        try_add_open_or_closed_pr(miner_eval, pr_raw, pr_state, lookback_date_filter)
                        continue

                    should_skip, skip_reason = should_skip_merged_pr(
                        pr_raw, repository_full_name, repo_config, lookback_date_filter
                    )

                    if should_skip:
                        bt.logging.debug(skip_reason or '')
                        continue

                    miner_eval.add_merged_pull_request(pr_raw)

                except Exception as e:
                    pr_number = pr_raw.get('number', '?')
                    bt.logging.warning(f'Error processing PR #{pr_number}, skipping: {e}')

            if not page_info.get('hasNextPage') or len(prs) == 0:
                break

            cursor = page_info.get('endCursor')

    except Exception as e:
        bt.logging.error(f'Unexpected error fetching PRs via GraphQL: {e}')

    bt.logging.info(
        f'Fetched {len(miner_eval.merged_pull_requests)} merged PRs, {len(miner_eval.open_pull_requests)} open PRs, '
        f'{len(miner_eval.closed_pull_requests)} closed'
    )
