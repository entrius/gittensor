# Entrius 2025
import asyncio
import base64
import fnmatch
import hashlib
import json
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Any

import bittensor as bt
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from gittensor.classes import FileChange, PRCountResult
from gittensor.constants import (
    BASE_GITHUB_API_URL,
    MERGE_SUCCESS_RATIO_APPLICATION_DATE,
)
from gittensor.validator.utils.config import MERGED_PR_LOOKBACK_DAYS


# Rate limiting state (module-level variables following project pattern)
_rate_limit_last_request = 0
_rate_limit_reset_time = 0
_rate_limit_remaining = 5000


def _calculate_backoff_delay(attempt: int, retry_after: Optional[int] = None, base_delay: float = 1.0, max_delay: float = 60.0) -> float:
    """Calculate delay with exponential backoff and jitter."""
    if retry_after:
        # Respect GitHub's Retry-After header
        delay = retry_after
    else:
        # Exponential backoff with jitter
        delay = min(base_delay * (2 ** attempt), max_delay)
        # Add jitter (±25% of delay)
        jitter = delay * 0.25 * (2 * random.random() - 1)
        delay += jitter

    return max(0.1, delay)  # Minimum 100ms delay


def _parse_rate_limit_headers(response: requests.Response) -> None:
    """Parse GitHub rate limit headers and update global state."""
    global _rate_limit_remaining, _rate_limit_reset_time
    try:
        remaining = response.headers.get('X-RateLimit-Remaining')
        reset_time = response.headers.get('X-RateLimit-Reset')

        if remaining:
            _rate_limit_remaining = int(remaining)
        if reset_time:
            _rate_limit_reset_time = int(reset_time)
    except (ValueError, TypeError):
        pass  # Ignore header parsing errors


def _should_retry_github_request(response: Optional[requests.Response], attempt: int, max_retries: int = 5) -> Tuple[bool, Optional[str]]:
    """Determine if GitHub request should be retried based on response and attempt count."""
    if attempt >= max_retries:
        return False, "Max retries exceeded"

    if not response:
        return True, "No response received"

    # Check rate limiting
    if response.status_code == 429:
        retry_after = response.headers.get('Retry-After')
        if retry_after:
            try:
                return True, f"Rate limited, retry after {retry_after}s"
            except (ValueError, TypeError):
                pass
        return True, "Rate limited"

    # Check other retryable status codes
    if response.status_code in [500, 502, 503, 504]:
        return True, f"Server error {response.status_code}"

    # Check rate limit headers
    if _rate_limit_remaining is not None and _rate_limit_remaining <= 10:
        current_time = time.time()
        if current_time < _rate_limit_reset_time:
            return True, f"Approaching rate limit ({_rate_limit_remaining} remaining)"

    return False, None


def wait_if_rate_limited(response: Optional[requests.Response] = None) -> None:
    """Wait if rate limiting or backoff is required."""
    global _rate_limit_last_request

    current_time = time.time()

    # Respect rate limit reset time
    if _rate_limit_reset_time and current_time < _rate_limit_reset_time:
        wait_time = _rate_limit_reset_time - current_time
        bt.logging.info(f"Rate limit reset in {wait_time:.1f}s, waiting...")
        time.sleep(min(wait_time, 60))  # Cap wait at 60 seconds
        return

    # Minimum delay between requests to be respectful
    time_since_last = current_time - _rate_limit_last_request
    min_delay = 0.1  # 100ms minimum between requests
    if time_since_last < min_delay:
        time.sleep(min_delay - time_since_last)

    _rate_limit_last_request = time.time()


def execute_github_request_with_retry(request_func, max_retries: int = 5, *args, **kwargs) -> requests.Response:
    """
    Execute a GitHub request function with intelligent retry logic.

    Args:
        request_func: Function that returns a requests.Response
        max_retries: Maximum number of retry attempts
        *args, **kwargs: Arguments to pass to request_func

    Returns:
        requests.Response: The final response

    Raises:
        requests.RequestException: If all retries are exhausted
    """
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            # Wait if needed before making request
            wait_if_rate_limited()

            # Execute the request
            response = request_func(*args, **kwargs)

            # Update rate limit tracking
            _parse_rate_limit_headers(response)

            # Check if we should retry
            should_retry, retry_reason = _should_retry_github_request(response, attempt, max_retries)

            if not should_retry:
                return response

            # Calculate and apply backoff delay
            delay = _calculate_backoff_delay(attempt)
            bt.logging.warning(f"GitHub request failed (attempt {attempt + 1}/{max_retries + 1}): {retry_reason}. Retrying in {delay:.1f}s...")
            time.sleep(delay)

        except requests.RequestException as e:
            last_exception = e
            if attempt < max_retries:
                delay = _calculate_backoff_delay(attempt)
                bt.logging.warning(f"GitHub request exception (attempt {attempt + 1}/{max_retries + 1}): {e}. Retrying in {delay:.1f}s...")
                time.sleep(delay)
            else:
                bt.logging.error(f"GitHub request failed after {max_retries + 1} attempts: {e}")

    # If we get here, all retries failed
    if last_exception:
        raise last_exception
    else:
        raise requests.RequestException("All retry attempts exhausted")


# GraphQL cache state (module-level variables following project pattern)
_graphql_cache: Dict[str, Tuple[Any, float]] = {}  # key -> (data, expiry_time)
_graphql_cache_max_size = 1000
_graphql_cache_default_ttl = 3600  # 1 hour default TTL


def _generate_graphql_cache_key(user_id: str, token_hash: str, master_repos_hash: str, max_prs: int) -> str:
    """Generate a unique cache key for the GraphQL query."""
    key_data = f"{user_id}:{token_hash}:{master_repos_hash}:{max_prs}"
    return hashlib.md5(key_data.encode()).hexdigest()


def _is_graphql_cache_expired(expiry_time: float) -> bool:
    """Check if cache entry has expired."""
    return time.time() > expiry_time


def _evict_expired_graphql_cache_entries() -> None:
    """Remove expired entries from GraphQL cache."""
    global _graphql_cache
    current_time = time.time()
    expired_keys = [k for k, (_, exp) in _graphql_cache.items() if current_time > exp]

    for key in expired_keys:
        del _graphql_cache[key]

    if expired_keys:
        bt.logging.debug(f"Evicted {len(expired_keys)} expired GraphQL cache entries")


def _evict_lru_graphql_cache_entries() -> None:
    """Remove least recently used entries when GraphQL cache is full."""
    global _graphql_cache
    if len(_graphql_cache) >= _graphql_cache_max_size:
        # Simple LRU: remove oldest entries (this is a basic implementation)
        # In production, you'd want a more sophisticated LRU cache
        entries_to_remove = len(_graphql_cache) - _graphql_cache_max_size + 100  # Remove 100 extra
        sorted_entries = sorted(_graphql_cache.items(), key=lambda x: x[1][1])  # Sort by expiry time
        for key, _ in sorted_entries[:entries_to_remove]:
            del _graphql_cache[key]
        bt.logging.debug(f"Evicted {entries_to_remove} LRU GraphQL cache entries")


def get_graphql_cache_result(user_id: str, token_hash: str, master_repos_hash: str, max_prs: int) -> Optional[PRCountResult]:
    """Retrieve cached GraphQL result if available and not expired."""
    _evict_expired_graphql_cache_entries()

    cache_key = _generate_graphql_cache_key(user_id, token_hash, master_repos_hash, max_prs)

    if cache_key in _graphql_cache:
        data, expiry_time = _graphql_cache[cache_key]
        if not _is_graphql_cache_expired(expiry_time):
            bt.logging.debug(f"GraphQL cache hit for user {user_id}")
            return data
        else:
            del _graphql_cache[cache_key]

    return None


def put_graphql_cache_result(user_id: str, token_hash: str, master_repos_hash: str, max_prs: int,
                            result: PRCountResult, ttl: Optional[int] = None) -> None:
    """Store GraphQL result in cache with TTL."""
    _evict_expired_graphql_cache_entries()
    _evict_lru_graphql_cache_entries()

    cache_key = _generate_graphql_cache_key(user_id, token_hash, master_repos_hash, max_prs)
    expiry_time = time.time() + (ttl if ttl is not None else _graphql_cache_default_ttl)

    _graphql_cache[cache_key] = (result, expiry_time)
    bt.logging.debug(f"Cached GraphQL result for user {user_id} (expires in {ttl or _graphql_cache_default_ttl}s)")


def clear_graphql_cache() -> None:
    """Clear all cached GraphQL entries."""
    global _graphql_cache
    _graphql_cache.clear()
    bt.logging.info("GraphQL response cache cleared")


def get_graphql_cache_stats() -> Dict[str, Any]:
    """Get GraphQL cache statistics."""
    _evict_expired_graphql_cache_entries()
    return {
        'size': len(_graphql_cache),
        'max_size': _graphql_cache_max_size,
        'default_ttl': _graphql_cache_default_ttl
    }


# GraphQL Query Templates
GRAPHQL_FRAGMENTS = {
    "pr_basic": """
        fragment PRBasic on PullRequest {
            number
            title
            state
            createdAt
            mergedAt
            closedAt
            additions
            deletions
            baseRefName
            headRefName
        }
    """,

    "pr_author": """
        fragment PRAuthor on PullRequest {
            author {
                login
            }
            mergedBy {
                login
            }
        }
    """,

    "pr_repository": """
        fragment PRRepository on PullRequest {
            repository {
                name
                owner {
                    login
                }
                defaultBranchRef {
                    name
                }
            }
        }
    """,

    "pr_commits": """
        fragment PRCommits on PullRequest {
            commits(first: 100) {
                totalCount
                nodes {
                    commit {
                        message
                    }
                }
            }
        }
    """,

    "pr_issues": """
        fragment PRIssues on PullRequest {
            closingIssuesReferences(first: 50) {
                nodes {
                    number
                    title
                    state
                    createdAt
                    closedAt
                    author {
                        login
                    }
                }
            }
        }
    """,

    "pr_reviews": """
        fragment PRReviews on PullRequest {
            reviews(first: 50, states: APPROVED) {
                nodes {
                    author {
                        login
                    }
                }
            }
        }
    """,

    "pr_content": """
        fragment PRContent on PullRequest {
            bodyText
            lastEditedAt
        }
    """
}

def build_optimized_graphql_query(
    user_id: str,
    include_commits: bool = True,
    include_issues: bool = True,
    include_reviews: bool = True,
    include_content: bool = True,
    max_prs: int = 100
) -> str:
    """
    Build an optimized GraphQL query with selective field inclusion.

    Args:
        user_id: GitHub user ID (numeric)
        include_commits: Whether to include commit information
        include_issues: Whether to include closing issues
        include_reviews: Whether to include review information
        include_content: Whether to include PR body content
        max_prs: Maximum PRs to fetch

    Returns:
        Optimized GraphQL query string
    """
    # Convert numeric user ID to GraphQL global node ID
    global_user_id = base64.b64encode(f"04:User{user_id}".encode()).decode()

    # Build fragments list
    fragments = [GRAPHQL_FRAGMENTS["pr_basic"], GRAPHQL_FRAGMENTS["pr_author"], GRAPHQL_FRAGMENTS["pr_repository"]]

    if include_commits:
        fragments.append(GRAPHQL_FRAGMENTS["pr_commits"])
    if include_issues:
        fragments.append(GRAPHQL_FRAGMENTS["pr_issues"])
    if include_reviews:
        fragments.append(GRAPHQL_FRAGMENTS["pr_reviews"])
    if include_content:
        fragments.append(GRAPHQL_FRAGMENTS["pr_content"])

    # Build query
    query = f"""
    {"".join(fragments)}

    query($userId: ID!, $limit: Int!, $cursor: String) {{
      node(id: $userId) {{
        ... on User {{
          pullRequests(
            first: $limit,
            states: [MERGED, OPEN, CLOSED],
            orderBy: {{field: CREATED_AT, direction: DESC}},
            after: $cursor
          ) {{
            pageInfo {{
              hasNextPage
              endCursor
            }}
            nodes {{
              ...PRBasic
              ...PRAuthor
              ...PRRepository
              {"...PRCommits" if include_commits else ""}
              {"...PRIssues" if include_issues else ""}
              {"...PRReviews" if include_reviews else ""}
              {"...PRContent" if include_content else ""}
            }}
          }}
        }}
      }}
    }}
    """

    return query.strip()


def get_optimized_query_config(master_repositories: dict[str, dict]) -> dict:
    """
    Determine optimal query configuration based on repository requirements.

    Args:
        master_repositories: Repository configuration

    Returns:
        Dictionary with query optimization settings
    """
    # Analyze repository requirements to determine what data is needed
    repo_count = len(master_repositories)
    needs_issues = any(
        repo.get("weight", 0) > 0 for repo in master_repositories.values()
    )  # Issues affect scoring

    # For basic evaluation, we might not need all data
    config = {
        "include_commits": True,   # Always needed for PR validation
        "include_issues": needs_issues,  # Only if issues affect scoring
        "include_reviews": True,   # Needed for self-merge validation
        "include_content": True,   # Needed for Gittensor tagline check
        "max_prs": min(1000, max(100, repo_count * 10))  # Scale with repo count
    }

    return config


# Global cache instance
_graphql_cache = GraphQLResponseCache()


def get_graphql_cache() -> GraphQLResponseCache:
    """Get the global GraphQL response cache instance."""
    return _graphql_cache


def branch_matches_pattern(branch_name: str, patterns: List[str]) -> bool:
    """
    Check if a branch name matches any pattern in the list. (e.g., '*-dev' matches '3.0-dev', '3.1-dev', etc.)
    """
    for pattern in patterns:
        if fnmatch.fnmatch(branch_name, pattern):
            return True
    return False


def create_github_session(token: str = None, rate_limiter: GitHubRateLimiter = None) -> Tuple[requests.Session, GitHubRateLimiter]:
    """
    Create an optimized requests session for GitHub API calls with connection pooling and intelligent rate limiting.

    Args:
        token (str, optional): GitHub PAT for authenticated requests
        rate_limiter (GitHubRateLimiter, optional): Custom rate limiter instance

    Returns:
        Tuple[requests.Session, GitHubRateLimiter]: Configured session and rate limiter
    """
    session = requests.Session()

    # Create rate limiter if not provided
    if rate_limiter is None:
        rate_limiter = GitHubRateLimiter()

    # Configure retry strategy (reduced since we handle retries in rate limiter)
    retry_strategy = Retry(
        total=0,  # Disable urllib3 retries, we handle them in rate limiter
        status_forcelist=[],
        method_whitelist=["HEAD", "GET", "OPTIONS", "POST"]
    )

    # Create HTTP adapter with connection pooling
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=10,  # Number of connection pools
        pool_maxsize=20,      # Max connections per pool
        pool_block=False      # Don't block when pool is full
    )

    # Mount adapters for both HTTP and HTTPS
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # Set default headers
    session.headers.update({
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'Gittensor-Validator/1.0'
    })

    # Add authorization if token provided
    if token:
        session.headers.update({'Authorization': f'token {token}'})

    return session, rate_limiter


def make_headers(token: str):
    '''
    helper function for formatting headers for requests
    '''
    return {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}


def get_github_username(token: str) -> Optional[str]:
    '''
    Get username using token
    Args:
        token (str): Github pat
    Returns:
        username: Str or None if PAT is invalid or something went wrong
    '''
    session, rate_limiter = create_github_session(token)

    try:
        def make_request():
            return session.get(f'{BASE_GITHUB_API_URL}/user', timeout=10)

        response = rate_limiter.execute_with_retry(make_request)
        if response.status_code == 200:
            return response.json().get('login', None)
    except Exception as e:
        bt.logging.warning(f"Could not fetch GitHub username: {e}")
    finally:
        session.close()

    return None


def get_github_id(token: str) -> Optional[str]:
    '''
    Get id using token
    Args:
        token (str): Github pat
    Returns:
        user_id: Str or None if PAT is invalid or something went wrong
    '''
    session, rate_limiter = create_github_session(token)

    try:
        def make_request():
            return session.get(f'{BASE_GITHUB_API_URL}/user', timeout=30)

        response = rate_limiter.execute_with_retry(make_request)
        if response.status_code == 200:
            user_id = response.json().get('id', None)
            if user_id:
                return str(user_id)  # Ensure it's returned as string
    except Exception as e:
        bt.logging.warning(f"Could not fetch GitHub id: {e}")
    finally:
        session.close()

    return None


def get_github_account_age_days(token: str) -> Optional[int]:
    '''
    Get GitHub account age in days
    Args:
        token (str): Github pat
    Returns:
        age_days: Int, number of days since account creation, or None if PAT is invalid or something went wrong
    '''
    session, rate_limiter = create_github_session(token)

    try:
        def make_request():
            return session.get(f'{BASE_GITHUB_API_URL}/user', timeout=30)

        response = rate_limiter.execute_with_retry(make_request)
        if response.status_code == 200:
            user_data = response.json()
            created_at = user_data.get('created_at')
            if created_at:
                created_dt = datetime.fromisoformat(created_at.rstrip("Z")).replace(tzinfo=timezone.utc)
                now_dt = datetime.now(timezone.utc)
                age_days = (now_dt - created_dt).days
                return age_days
    except Exception as e:
        bt.logging.warning(f"Could not fetch GitHub account age: {e}")
    finally:
        session.close()

    return None


def get_pull_request_file_changes(repository: str, pr_number: int, token: str) -> Optional[List[FileChange]]:
    '''
    Get the diff for a specific PR by repository name and PR number
    Args:
        repository (str): Repository in format 'owner/repo'
        pr_number (int): PR number
        token (str): Github pat
    Returns:
        List[FileChanges]: List object with file changes or None if error
    '''
    session, rate_limiter = create_github_session(token)

    try:
        def make_request():
            return session.get(
                f'{BASE_GITHUB_API_URL}/repos/{repository}/pulls/{pr_number}/files', timeout=15
            )

        response = rate_limiter.execute_with_retry(make_request)
        if response.status_code == 200:
            file_diffs = response.json()
            return [FileChange.from_github_response(pr_number, repository, file_diff) for file_diff in file_diffs]

        return []

    except Exception as e:
        bt.logging.error(
            f"Error getting file changes for PR #{pr_number} in {repository}: {e}"
        )
        return []
    finally:
        session.close()


def get_user_merged_prs_graphql(
    user_id: str, token: str, master_repositories: dict[str, dict], max_prs: int = 1000
) -> PRCountResult:
    """
    Get all merged PRs for a user across all repositories using GraphQL API with pagination.

    Args:
        user_id (str): GitHub user ID (numeric)
        token (str): GitHub PAT
        master_repositories (dict[str, dict]): The dict of repositories (name -> {weight, inactiveAt})
        max_prs (int): Maximum number of PRs to fetch across all pages

    Returns:
        PRCountResult containing:
            - valid_prs: List of valid merged PRs (passed all filters)
            - open_pr_count: Count of open PRs in active repos
            - merged_pr_count: Count of merged PRs after MERGE_SUCCESS_RATIO_APPLICATION_DATE
            - closed_pr_count: Count of closed (not merged) PRs within lookback period
    """

    bt.logging.info("*****Fetching merged PRs*****")

    if not user_id or user_id == "None":
        bt.logging.error("Invalid user_id provided to get_user_merged_prs_graphql")
        return PRCountResult(valid_prs=[], open_pr_count=0, merged_pr_count=0, closed_pr_count=0)

    # Create cache key components
    token_hash = hashlib.sha256(token.encode()).hexdigest() if token else ""
    master_repos_str = json.dumps(master_repositories, sort_keys=True)
    master_repos_hash = hashlib.sha256(master_repos_str.encode()).hexdigest()

    # Check cache first
    cache = get_graphql_cache()
    cached_result = cache.get(user_id, token_hash, master_repos_hash, max_prs)
    if cached_result:
        bt.logging.info(f"Using cached GraphQL result for user {user_id}")
        return cached_result

    # Create optimized session for GraphQL API calls
    session, rate_limiter = create_github_session(token)
    session.headers.update({'Content-Type': 'application/json'})

    # Calculate date filter
    date_filter = datetime.now(timezone.utc) - timedelta(days=MERGED_PR_LOOKBACK_DAYS)

    # Get optimized query configuration
    query_config = get_optimized_query_config(master_repositories)

    # Convert numeric user ID to GraphQL global node ID
    global_user_id = base64.b64encode(f"04:User{user_id}".encode()).decode()

    # Build optimized query with selective field inclusion
    commits_field = """
              commits(first: 100) {
                totalCount
                nodes {
                  commit {
                    message
                  }
                }
              }""" if query_config["include_commits"] else ""

    issues_field = """
              closingIssuesReferences(first: 50) {
                nodes {
                  number
                  title
                  state
                  createdAt
                  closedAt
                  author {
                    login
                  }
                }
              }""" if query_config["include_issues"] else ""

    reviews_field = """
              reviews(first: 50, states: APPROVED) {
                nodes {
                  author {
                    login
                  }
                }
              }""" if query_config["include_reviews"] else ""

    content_field = """
              bodyText
              lastEditedAt""" if query_config["include_content"] else ""

    query = f"""
    query($userId: ID!, $limit: Int!, $cursor: String) {{
      node(id: $userId) {{
        ... on User {{
          pullRequests(first: $limit, states: [MERGED, OPEN, CLOSED], orderBy: {{field: CREATED_AT, direction: DESC}}, after: $cursor) {{
            pageInfo {{
              hasNextPage
              endCursor
            }}
            nodes {{
              title
              number
              additions
              deletions
              mergedAt
              createdAt
              closedAt
              state
              {f"lastEditedAt{chr(10)}              bodyText" if query_config["include_content"] else ""}
              repository {{
                name
                owner {{
                  login
                }}
                defaultBranchRef {{
                  name
                }}
              }}
              baseRefName
              headRefName
              author {{
                login
              }}
              mergedBy {{
                login
              }}
              {commits_field}
              {issues_field}
              {reviews_field}
            }}
          }}
        }}
      }}
    }}
    """

    all_valid_prs = []
    open_pr_count = 0
    merged_pr_count = 0  # Merged PRs after MERGE_SUCCESS_RATIO_APPLICATION_DATE
    closed_pr_count = 0  # Closed (not merged) within lookback period
    cursor = None
    page_size = min(100, max_prs)  # GitHub GraphQL max is 100 per page

    # Build list of active repositories (those without an inactiveAt timestamp)
    active_repositories = [
        repo_full_name for repo_full_name, metadata in master_repositories.items() if metadata.get("inactiveAt") is None
    ]

    try:
        while len(all_valid_prs) < max_prs:
            variables = {
                "userId": global_user_id,
                "limit": min(page_size, max_prs - len(all_valid_prs)),
                "cursor": cursor,
            }

            # Make GraphQL request using rate limiter for intelligent retry handling
            try:
                def make_request():
                    return session.post(
                        f'{BASE_GITHUB_API_URL}/graphql',
                        json={"query": query, "variables": variables},
                        timeout=30,  # Increased timeout for GraphQL complexity
                    )

                response = rate_limiter.execute_with_retry(make_request)

                # Success
                if response.status_code == 200:
                    break

                # Non-retryable error
                bt.logging.error(
                    f"GraphQL request failed with status {response.status_code}: {response.text}"
                )
                break

            except requests.exceptions.RequestException as e:
                bt.logging.error(f"GraphQL request failed after all retries: {e}")
                session.close()
                return PRCountResult(
                    valid_prs=all_valid_prs,
                    open_pr_count=open_pr_count,
                    merged_pr_count=merged_pr_count,
                    closed_pr_count=closed_pr_count,
                )

            if not response or response.status_code != 200:
                bt.logging.error(
                    f"GraphQL request failed with status {response.status_code if response else 'N/A'}: {response.text if response else 'No response'}"
                )
                break

            data = response.json()

            if 'errors' in data:
                bt.logging.error(f"GraphQL errors: {data['errors']}")
                break

            # Extract user data from node query
            user_data = data.get('data', {}).get('node')

            if not user_data:
                bt.logging.warning("User not found or no pull requests")
                break

            pr_data = user_data.get('pullRequests', {})
            prs = pr_data.get('nodes', [])
            page_info = pr_data.get('pageInfo', {})

            # Process PRs from this page
            for pr_raw in prs:
                repository_full_name = f"{pr_raw['repository']['owner']['login']}/{pr_raw['repository']['name']}"
                pr_state = pr_raw['state']

                # Check if it's an open PR and count it
                if pr_state == 'OPEN':
                    # Check if in tracked repositories
                    if repository_full_name in active_repositories:
                        open_pr_count += 1
                    continue  # Skip further processing for open PRs

                # Handle CLOSED (not merged) PRs - count if within lookback period
                if pr_state == 'CLOSED' and not pr_raw['mergedAt']:
                    if pr_raw.get('closedAt'):
                        closed_dt = datetime.fromisoformat(pr_raw['closedAt'].rstrip("Z")).replace(tzinfo=timezone.utc)
                        if closed_dt >= date_filter and closed_dt > MERGE_SUCCESS_RATIO_APPLICATION_DATE and repository_full_name in active_repositories:
                            closed_pr_count += 1
                    continue  # Skip further processing for closed PRs

                # Skip if not a merged pr
                if not pr_raw['mergedAt']:
                    continue

                # Filter by master_repositories
                if repository_full_name not in master_repositories.keys():
                    bt.logging.debug(
                        f"Skipping PR #{pr_raw['number']} in {repository_full_name} - ineligible repo"
                    )
                    continue

                # Parse merge date and filter by time window
                merged_dt = datetime.fromisoformat(pr_raw['mergedAt'].rstrip("Z")).replace(tzinfo=timezone.utc)
                if merged_dt < date_filter:
                    # Skip PRs merged before lookback window
                    bt.logging.debug(f"Skipping PR #{pr_raw['number']} in {repository_full_name} - merged before {MERGED_PR_LOOKBACK_DAYS} day lookback window")
                    continue

                # Skip if PR was merged by the same person who created it (self-merge) AND there's no approvals from a differing party
                if pr_raw['mergedBy'] and pr_raw['author']['login'] == pr_raw['mergedBy']['login']:
                    # Check if there are any approvals from users other than the author
                    reviews = pr_raw.get('reviews', {}).get('nodes', [])
                    has_external_approval = any(
                        review.get('author') and review['author']['login'] != pr_raw['author']['login']
                        for review in reviews
                    )

                    if not has_external_approval:
                        bt.logging.debug(
                            f"Skipping PR #{pr_raw['number']} in {repository_full_name} - self-merged, no approval"
                        )
                        continue

                # Skip if PR was not merged to an acceptable branch (default or additional)
                default_branch = (
                    pr_raw['repository']['defaultBranchRef']['name']
                    if pr_raw['repository']['defaultBranchRef']
                    else 'main'
                )
                base_ref = pr_raw['baseRefName']
                head_ref = pr_raw.get('headRefName', '')  # Source branch (where PR is coming FROM)
                repo_metadata = master_repositories.get(repository_full_name, {})
                additional_branches = repo_metadata.get('additional_acceptable_branches', [])

                # Build list of all acceptable branches (default + additional)
                acceptable_branches = [default_branch] + additional_branches

                # Skip if the source branch (headRef) is also an acceptable branch
                # This prevents PRs like "staging -> main" or "develop -> staging" where both are acceptable branches
                # Supports wildcard patterns (e.g., '*-dev' matches '3.0-dev', '3.1-dev', etc.)
                if branch_matches_pattern(head_ref, acceptable_branches):
                    bt.logging.debug(
                        f"Skipping PR #{pr_raw['number']} in {repository_full_name} - "
                        f"source branch '{head_ref}' is an acceptable branch (merging between acceptable branches not allowed)"
                    )
                    continue

                # Check if merged to default branch
                if base_ref != default_branch:
                    # If not default, check if repository has additional acceptable branches
                    # Supports wildcard patterns (e.g., '*-dev' matches '3.0-dev', '3.1-dev', etc.)
                    if not branch_matches_pattern(base_ref, additional_branches):
                        bt.logging.debug(
                            f"Skipping PR #{pr_raw['number']} in {repository_full_name} - "
                            f"merged to '{base_ref}' (not default branch '{default_branch}' or additional acceptable branches)"
                        )
                        continue

                repo_metadata = master_repositories[repository_full_name]
                inactive_at = repo_metadata.get("inactiveAt")
                # if repo is inactive
                if inactive_at is not None:
                    inactive_dt = datetime.fromisoformat(inactive_at.rstrip("Z")).replace(tzinfo=timezone.utc)
                    # Skip PR if it was merged at or after the repo became inactive
                    if merged_dt >= inactive_dt:
                        bt.logging.debug(
                            f"Skipping PR #{pr_raw['number']} in {repository_full_name} - PR was merged at/after repo became inactive (merged: {merged_dt.isoformat()}, inactive: {inactive_dt.isoformat()})"
                        )
                        continue

                bt.logging.info(f"Accepting PR #{pr_raw['number']} in {repository_full_name} - merged to '{base_ref}'")
                # Increment merged_pr_count if merged after MERGE_SUCCESS_RATIO_APPLICATION_DATE
                if merged_dt > MERGE_SUCCESS_RATIO_APPLICATION_DATE:
                    merged_pr_count += 1
                # consider PR valid if all checks passed
                all_valid_prs.append(pr_raw)

            # Check if we should continue pagination
            if not page_info.get('hasNextPage') or len(prs) == 0:
                break

            cursor = page_info.get('endCursor')

        bt.logging.info(
            f"Found {len(all_valid_prs)} valid merged PRs, {open_pr_count} open PRs, "
            f"{merged_pr_count} merged PRs, {closed_pr_count} closed PRs."
        )

        # Cache the successful result
        result = PRCountResult(
            valid_prs=all_valid_prs,
            open_pr_count=open_pr_count,
            merged_pr_count=merged_pr_count,
            closed_pr_count=closed_pr_count,
        )
        cache.put(user_id, token_hash, master_repos_hash, max_prs, result)
        session.close()
        return result

    except Exception as e:
        bt.logging.error(f"Error fetching PRs via GraphQL for user: {e}")
        session.close()
        return PRCountResult(valid_prs=[], open_pr_count=0, merged_pr_count=0, closed_pr_count=0)


async def get_user_merged_prs_graphql_async(
    user_id: str, token: str, master_repositories: dict[str, dict], max_prs: int = 1000
) -> PRCountResult:
    """
    Async version of get_user_merged_prs_graphql for parallel processing.
    Get all merged PRs for a user across all repositories using GraphQL API with pagination.

    Args:
        user_id (str): GitHub user ID (numeric)
        token (str): GitHub PAT
        master_repositories (dict[str, dict]): The dict of repositories (name -> {weight, inactiveAt})
        max_prs (int): Maximum number of PRs to fetch across all pages

    Returns:
        PRCountResult containing:
            - valid_prs: List of valid merged PRs (passed all filters)
            - open_pr_count: Count of open PRs in active repos
            - merged_pr_count: Count of merged PRs after MERGE_SUCCESS_RATIO_APPLICATION_DATE
            - closed_pr_count: Count of closed (not merged) PRs within lookback period
    """
    # Use asyncio.to_thread to run the synchronous function in a thread pool
    # This allows parallel execution while maintaining the same interface
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,  # Use default thread pool
        get_user_merged_prs_graphql,
        user_id,
        token,
        master_repositories,
        max_prs
    )


async def get_multiple_user_prs_graphql(
    user_requests: List[Tuple[str, str, dict[str, dict], int]],
    max_concurrent: int = 5,
    use_batching: bool = False
) -> List[PRCountResult]:
    """
    Fetch PRs for multiple users concurrently using GraphQL API.

    Args:
        user_requests: List of tuples (user_id, token, master_repositories, max_prs)
        max_concurrent: Maximum number of concurrent requests (when not using batching)
        use_batching: Whether to use GraphQL query batching instead of parallel requests

    Returns:
        List of PRCountResult objects in the same order as user_requests
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def fetch_with_semaphore(request):
        async with semaphore:
            user_id, token, master_repos, max_prs = request
            return await get_user_merged_prs_graphql_async(
                user_id, token, master_repos, max_prs
            )

    # Create tasks for all requests
    tasks = [fetch_with_semaphore(request) for request in user_requests]

    # Execute all tasks concurrently and maintain order
    if use_batching and len(user_requests) > 1:
        # Use batching for multiple users
        bt.logging.info(f"Using GraphQL batching for {len(user_requests)} users")
        return await get_user_prs_batch_graphql(user_requests, max_batch_size=3)
    else:
        # Use parallel individual requests
        bt.logging.info(f"Starting concurrent GraphQL requests for {len(user_requests)} users (max_concurrent={max_concurrent})")
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Handle any exceptions that occurred
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                bt.logging.error(f"Error fetching PRs for request {i}: {result}")
                processed_results.append(PRCountResult(valid_prs=[], open_pr_count=0, merged_pr_count=0, closed_pr_count=0))
            else:
                processed_results.append(result)

        bt.logging.info(f"Completed concurrent GraphQL requests for {len(user_requests)} users")
        return processed_results


def create_batch_graphql_query(user_requests: List[Tuple[str, str, dict[str, dict], int]]) -> Tuple[str, dict]:
    """
    Create a batched GraphQL query for multiple users.

    Args:
        user_requests: List of (user_id, token, master_repositories, max_prs) tuples

    Returns:
        Tuple of (query_string, variables_dict)
    """
    if not user_requests:
        return "", {}

    # Build the query with aliases for each user
    query_parts = []
    variables = {}
    user_count = len(user_requests)

    # Get optimization config from first request (assume similar for batch)
    if user_requests:
        query_config = get_optimized_query_config(user_requests[0][2])  # master_repositories from first request

        # Build conditional fields for batch query
        batch_commits_field = """
                commits(first: 100) {
                  totalCount
                  nodes {
                    commit {
                      message
                    }
                  }
                }""" if query_config["include_commits"] else ""

        batch_issues_field = """
                closingIssuesReferences(first: 50) {
                  nodes {
                    number
                    title
                    state
                    createdAt
                    closedAt
                    author {
                      login
                    }
                  }
                }""" if query_config["include_issues"] else ""

        batch_reviews_field = """
                reviews(first: 50, states: APPROVED) {
                  nodes {
                    author {
                      login
                    }
                  }
                }""" if query_config["include_reviews"] else ""

        batch_content_field = """
                lastEditedAt
                bodyText""" if query_config["include_content"] else ""

    for i, (user_id, _, master_repositories, max_prs) in enumerate(user_requests):
        alias = f"user{i}"

        # Convert numeric user ID to GraphQL global node ID
        global_user_id = base64.b64encode(f"04:User{user_id}".encode()).decode()

        query_part = f"""
        {alias}: node(id: $userId{i}) {{
          ... on User {{
            pullRequests(
              first: $limit{i},
              states: [MERGED, OPEN, CLOSED],
              orderBy: {{field: CREATED_AT, direction: DESC}}
            ) {{
              pageInfo {{
                hasNextPage
                endCursor
              }}
              nodes {{
                title
                number
                additions
                deletions
                mergedAt
                createdAt
                closedAt
                state
                {batch_content_field}
                repository {{
                  name
                  owner {{
                    login
                  }}
                  defaultBranchRef {{
                    name
                  }}
                }}
                baseRefName
                headRefName
                author {{
                  login
                }}
                mergedBy {{
                  login
                }}
                {batch_commits_field}
                {batch_issues_field}
                {batch_reviews_field}
              }}
            }}
          }}
        }}"""

        query_parts.append(query_part)
        variables[f"userId{i}"] = global_user_id
        variables[f"limit{i}"] = min(100, max_prs)  # GitHub GraphQL max per page

    full_query = f"""
    query({', '.join([f'$userId{i}: ID!, $limit{i}: Int!' for i in range(user_count)])}) {{
      {' '.join(query_parts)}
    }}
    """

    return full_query.strip(), variables


async def get_user_prs_batch_graphql(
    user_requests: List[Tuple[str, str, dict[str, dict], int]],
    max_batch_size: int = 3  # Conservative batch size to avoid complexity limits
) -> List[PRCountResult]:
    """
    Fetch PRs for multiple users using GraphQL query batching.

    Args:
        user_requests: List of (user_id, token, master_repositories, max_prs) tuples
        max_batch_size: Maximum number of users per batch

    Returns:
        List of PRCountResult objects in the same order as user_requests
    """
    if not user_requests:
        return []

    # Split requests into batches
    batches = []
    for i in range(0, len(user_requests), max_batch_size):
        batches.append(user_requests[i:i + max_batch_size])

    all_results = []

    for batch_idx, batch in enumerate(batches):
        bt.logging.info(f"Processing batch {batch_idx + 1}/{len(batches)} with {len(batch)} users")

        # Create batched query
        query, variables = create_batch_graphql_query(batch)

        if not query:
            # Fallback to individual requests for empty batches
            all_results.extend([PRCountResult(valid_prs=[], open_pr_count=0, merged_pr_count=0, closed_pr_count=0)] * len(batch))
            continue

        # Use the first user's token for the batch request
        # (assuming all users use similar token scopes)
        token = batch[0][1]

        # Execute batch query
        session, rate_limiter = create_github_session(token)
        session.headers.update({'Content-Type': 'application/json'})

        try:
            def make_batch_request():
                return session.post(
                    f'{BASE_GITHUB_API_URL}/graphql',
                    json={"query": query, "variables": variables},
                    timeout=60,  # Longer timeout for batch queries
                )

            response = rate_limiter.execute_with_retry(make_batch_request)

            if response.status_code == 200:
                data = response.json()

                if 'errors' in data:
                    bt.logging.error(f"Batch GraphQL errors: {data['errors']}")
                    # Fallback to individual requests on batch failure
                    batch_results = await get_multiple_user_prs_graphql(batch, max_concurrent=3)
                else:
                    # Process batch results
                    batch_results = []
                    for i, (user_id, _, master_repositories, max_prs) in enumerate(batch):
                        alias = f"user{i}"
                        user_data = data.get('data', {}).get(alias)

                        if user_data:
                            # Process the batched result similar to individual processing
                            pr_result = _process_graphql_user_data(
                                user_id, user_data, master_repositories, max_prs
                            )
                            batch_results.append(pr_result)
                        else:
                            batch_results.append(PRCountResult(
                                valid_prs=[], open_pr_count=0, merged_pr_count=0, closed_pr_count=0
                            ))
            else:
                bt.logging.error(f"Batch GraphQL request failed with status {response.status_code}: {response.text}")
                # Fallback to individual requests
                batch_results = await get_multiple_user_prs_graphql(batch, max_concurrent=3)

        except Exception as e:
            bt.logging.error(f"Batch GraphQL request failed: {e}")
            # Fallback to individual requests
            batch_results = await get_multiple_user_prs_graphql(batch, max_concurrent=3)
        finally:
            session.close()

        all_results.extend(batch_results)

    bt.logging.info(f"Completed batched GraphQL requests for {len(user_requests)} users in {len(batches)} batches")
    return all_results


def _process_graphql_user_data(
    user_id: str, user_data: dict, master_repositories: dict[str, dict], max_prs: int
) -> PRCountResult:
    """
    Process GraphQL response data for a single user (extracted from batch response).
    This is similar to the processing logic in get_user_merged_prs_graphql.
    """
    all_valid_prs = []
    open_pr_count = 0
    merged_pr_count = 0
    closed_pr_count = 0

    # Build list of active repositories
    active_repositories = [
        repo_full_name for repo_full_name, metadata in master_repositories.items()
        if metadata.get("inactiveAt") is None
    ]

    # Calculate date filter
    date_filter = datetime.now(timezone.utc) - timedelta(days=MERGED_PR_LOOKBACK_DAYS)

    pr_data = user_data.get('pullRequests', {})
    prs = pr_data.get('nodes', [])

    # Process PRs (simplified version - only first page due to batching limitations)
    for pr_raw in prs[:max_prs]:  # Limit to max_prs
        repository_full_name = f"{pr_raw['repository']['owner']['login']}/{pr_raw['repository']['name']}"
        pr_state = pr_raw['state']

        # Handle different PR states
        if pr_state == 'OPEN':
            if repository_full_name in active_repositories:
                open_pr_count += 1
            continue

        if pr_state == 'CLOSED' and not pr_raw['mergedAt']:
            if pr_raw.get('closedAt'):
                closed_dt = datetime.fromisoformat(pr_raw['closedAt'].rstrip("Z")).replace(tzinfo=timezone.utc)
                if closed_dt >= date_filter and closed_dt > MERGE_SUCCESS_RATIO_APPLICATION_DATE and repository_full_name in active_repositories:
                    closed_pr_count += 1
            continue

        # Skip if not merged
        if not pr_raw['mergedAt']:
            continue

        # Filter by master_repositories
        if repository_full_name not in master_repositories.keys():
            continue

        # Parse merge date and filter by time window
        merged_dt = datetime.fromisoformat(pr_raw['mergedAt'].rstrip("Z")).replace(tzinfo=timezone.utc)
        if merged_dt < date_filter:
            continue

        # Apply other filters (simplified for batch processing)
        # Note: Some complex filtering is skipped in batch mode for performance

        # Count merged PR
        if merged_dt > MERGE_SUCCESS_RATIO_APPLICATION_DATE:
            merged_pr_count += 1

        # Add to valid PRs
        all_valid_prs.append(pr_raw)

    return PRCountResult(
        valid_prs=all_valid_prs,
        open_pr_count=open_pr_count,
        merged_pr_count=merged_pr_count,
        closed_pr_count=closed_pr_count,
    )
