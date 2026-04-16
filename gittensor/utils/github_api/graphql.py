# Entrius 2025
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import bittensor as bt
import requests

from gittensor.constants import BASE_GITHUB_API_URL

# core github graphql query
QUERY = """
    query($userId: ID!, $limit: Int!, $cursor: String) {
      node(id: $userId) {
        ... on User {
          issues(states: [OPEN]) { totalCount }
          pullRequests(first: $limit, states: [MERGED, OPEN, CLOSED], orderBy: {field: CREATED_AT, direction: DESC}, after: $cursor) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              title
              number
              additions
              deletions
              mergedAt
              createdAt
              closedAt
              lastEditedAt
              bodyText
              state
              commits {
                totalCount
              }
              repository {
                name
                owner {
                  login
                }
                defaultBranchRef {
                  name
                }
              }
              headRepository {
                name
                owner {
                  login
                }
              }
              baseRefName
              baseRefOid
              headRefName
              headRefOid
              author {
                login
              }
              authorAssociation
              mergedBy {
                login
              }
              closingIssuesReferences(first: 3) {
                nodes {
                  number
                  title
                  state
                  createdAt
                  closedAt
                  updatedAt
                  author {
                    login
                    ... on User { databaseId }
                  }
                  authorAssociation
                  userContentEdits(first: 1) {
                    nodes { editedAt }
                  }
                  timelineItems(itemTypes: [RENAMED_TITLE_EVENT], last: 1) {
                    nodes {
                      ... on RenamedTitleEvent { createdAt }
                    }
                  }
                }
              }
              reviews(first: 3, states: APPROVED) {
                nodes {
                  author {
                    login
                  }
                }
              }
              timelineItems(itemTypes: [LABELED_EVENT], last: 1) {
                nodes {
                  ... on LabeledEvent {
                    label { name }
                    createdAt
                  }
                }
              }
            }
          }
        }
      }
    }
    """


def execute_graphql_query(
    query: str,
    variables: Dict[str, Any],
    token: str,
    max_attempts: int = 8,
    timeout: int = 30,
) -> Optional[Dict[str, Any]]:
    """
    Execute a GraphQL query with retry logic and backoff.

    Args:
        query: The GraphQL query string
        variables: Query variables
        token: GitHub PAT for authentication
        max_attempts: Maximum retry attempts (default 6)
        timeout: Request timeout in seconds (default 30)

    Returns:
        Parsed JSON response data, or None if all attempts failed
    """
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    for attempt in range(max_attempts):
        try:
            response = requests.post(
                f'{BASE_GITHUB_API_URL}/graphql',
                headers=headers,
                json={'query': query, 'variables': variables},
                timeout=timeout,
            )

            if response.status_code == 200:
                return response.json()

            # Retry on failure
            if attempt < (max_attempts - 1):
                backoff_delay = min(5 * (2**attempt), 30)  # max of 30 second wait between retries
                bt.logging.warning(
                    f'GraphQL request failed with status {response.status_code} '
                    f'(attempt {attempt + 1}/{max_attempts}), retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)
            else:
                bt.logging.error(
                    f'GraphQL request failed with status {response.status_code} '
                    f'after {max_attempts} attempts: {response.text}'
                )

        except requests.exceptions.RequestException as e:
            if attempt < (max_attempts - 1):
                backoff_delay = min(5 * (2**attempt), 30)
                bt.logging.warning(
                    f'GraphQL request exception (attempt {attempt + 1}/{max_attempts}), '
                    f'retrying in {backoff_delay}s: {e}'
                )
                time.sleep(backoff_delay)
            else:
                bt.logging.error(f'GraphQL request failed after {max_attempts} attempts: {e}')

    return None


@dataclass
class GraphQLPageResult:
    """Result of a paginated GraphQL query."""

    response: Optional[requests.Response]
    page_size: int  # actual page size used (may have been reduced on retry)


def get_github_graphql_query(
    token: str,
    global_user_id: str,
    merged_pr_count: int,
    max_prs: int,
    cursor: Optional[str],
    page_size: Optional[int] = None,
) -> GraphQLPageResult:
    """
    Get all merged PRs for a user across all repositories using GraphQL API with pagination.

    Handles RESOURCE_LIMITS_EXCEEDED (HTTP 200 with errors in body) by halving
    page size and retrying, same pattern as 502/503/504 handling.

    Args:
        token: GitHub PAT
        global_user_id: Converted numeric user ID to GraphQL global node ID
        merged_pr_count: Count of all validated and merged PRs
        max_prs: Maximum number of PRs to fetch across all pages
        cursor: Pagination cursor (where query left off last), None for first page
        page_size: Override page size (used when retrying with smaller pages)

    Returns:
        GraphQLPageResult with response and the page size actually used
    """

    max_attempts = 8
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    limit = page_size if page_size is not None else min(100, max_prs - merged_pr_count)

    for attempt in range(max_attempts):
        variables = {
            'userId': global_user_id,
            'limit': limit,
            'cursor': cursor,
        }
        try:
            response = requests.post(
                f'{BASE_GITHUB_API_URL}/graphql',
                headers=headers,
                json={'query': QUERY, 'variables': variables},
                timeout=30,
            )

            if response.status_code == 200:
                # Check for RESOURCE_LIMITS_EXCEEDED in response body (GitHub returns 200 with errors)
                try:
                    data = response.json()
                except Exception:
                    return GraphQLPageResult(response=response, page_size=limit)

                if _has_resource_limit_errors(data):
                    if attempt < (max_attempts - 1):
                        old_limit = limit
                        limit = max(limit // 2, 10)
                        backoff_delay = min(2 * (2**attempt), 15)
                        bt.logging.warning(
                            f'GraphQL RESOURCE_LIMITS_EXCEEDED (attempt {attempt + 1}/{max_attempts}), '
                            f'page size {old_limit} -> {limit}, retrying in {backoff_delay}s...'
                        )
                        time.sleep(backoff_delay)
                        continue
                    else:
                        bt.logging.error(
                            f'GraphQL RESOURCE_LIMITS_EXCEEDED at page size {limit} after {max_attempts} attempts'
                        )
                        return GraphQLPageResult(response=None, page_size=limit)

                return GraphQLPageResult(response=response, page_size=limit)

            # HTTP error - log and retry
            if attempt < (max_attempts - 1):
                backoff_delay = min(5 * (2**attempt), 30)
                if response.status_code in (502, 503, 504):
                    limit = max(limit // 2, 10)
                    bt.logging.warning(
                        f'GraphQL request for PRs failed with status {response.status_code} '
                        f'(attempt {attempt + 1}/{max_attempts}), page size set to {limit}, retrying in {backoff_delay}s...'
                    )
                else:
                    bt.logging.warning(
                        f'GraphQL request for PRs failed with status {response.status_code} '
                        f'(attempt {attempt + 1}/{max_attempts}), retrying in {backoff_delay}s...'
                    )
                time.sleep(backoff_delay)
            else:
                bt.logging.error(
                    f'GraphQL request for PRs failed with status {response.status_code} after {max_attempts} attempts: {response.text}'
                )

        except requests.exceptions.RequestException as e:
            if attempt < (max_attempts - 1):
                backoff_delay = min(5 * (2**attempt), 30)
                bt.logging.warning(
                    f'GraphQL request connection error (attempt {attempt + 1}/{max_attempts}): {e}, retrying in {backoff_delay}s...'
                )
                time.sleep(backoff_delay)
            else:
                bt.logging.error(f'GraphQL request failed after {max_attempts} attempts: {e}')
                return GraphQLPageResult(response=None, page_size=limit)

    return GraphQLPageResult(response=None, page_size=limit)


def _has_resource_limit_errors(data: Dict) -> bool:
    """Check if a GraphQL response contains RESOURCE_LIMITS_EXCEEDED errors."""
    errors = data.get('errors')
    if not errors or not isinstance(errors, list):
        return False
    return any(isinstance(e, dict) and e.get('type') == 'RESOURCE_LIMITS_EXCEEDED' for e in errors)
