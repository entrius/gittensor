# The MIT License (MIT)
# Copyright 2025 Entrius

"""GitHub PR/issue solution detection for issue competitions."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import bittensor as bt
import requests

from gittensor.constants import BASE_GITHUB_API_URL
from gittensor.utils.github_api_tools import get_github_username, make_headers


@dataclass
class SolutionDetectionResult:
    """Result of checking if a GitHub issue has been solved."""

    issue_id: int
    is_solved: bool
    solved_by_competitor: bool
    solver_hotkey: Optional[str] = None
    solving_pr_url: Optional[str] = None
    solver_github_username: Optional[str] = None
    solved_at: Optional[datetime] = None
    error: Optional[str] = None


def detect_issue_solution(
    github_tokens: Dict[str, str],
    repository_full_name: str,
    issue_number: int,
    competitor_hotkeys: List[str],
    competition_start_time: datetime,
    submission_window_end: datetime,
) -> SolutionDetectionResult:
    """
    Check if a GitHub issue has been solved by a competitor.

    This function validates that:
    1. The issue is closed
    2. The issue has a linked PR that was merged
    3. The PR was merged AFTER the submission window ended
    4. The PR author matches a competitor's GitHub username

    Args:
        github_tokens: Dict mapping hotkey -> miner's github_access_token
        repository_full_name: Repository in "owner/repo" format
        issue_number: Issue number to check
        competitor_hotkeys: List of hotkeys for competing miners
        competition_start_time: When the competition started
        submission_window_end: When the submission window closed (PRs must be
                               created BEFORE this, but merged AFTER)

    Returns:
        SolutionDetectionResult with detection status
    """
    result = SolutionDetectionResult(
        issue_id=issue_number,
        is_solved=False,
        solved_by_competitor=False,
    )

    # Get a working token (any competitor's token will do for reading)
    working_token = None
    for hotkey in competitor_hotkeys:
        if hotkey in github_tokens and github_tokens[hotkey]:
            working_token = github_tokens[hotkey]
            break

    if not working_token:
        bt.logging.warning(f'No valid GitHub token available for solution detection')
        result.error = 'No GitHub token available'
        return result

    try:
        # Step 1: Check if issue is closed
        issue_data = _get_issue_data(repository_full_name, issue_number, working_token)
        if not issue_data:
            result.error = 'Failed to fetch issue data'
            return result

        if issue_data.get('state') != 'closed':
            bt.logging.debug(f'Issue #{issue_number} is not closed')
            return result

        # Step 2: Get linked/closing PRs
        linked_prs = _get_linked_prs(repository_full_name, issue_number, working_token)
        if not linked_prs:
            bt.logging.debug(f'Issue #{issue_number} has no linked PRs')
            return result

        # Step 3: Build mapping of GitHub username -> hotkey for competitors
        competitor_usernames = _build_username_mapping(github_tokens, competitor_hotkeys)

        # Step 4: Check each linked PR
        for pr_data in linked_prs:
            solution_result = _check_pr_solution(
                pr_data,
                repository_full_name,
                working_token,
                competitor_usernames,
                competition_start_time,
                submission_window_end,
            )

            if solution_result.is_solved and solution_result.solved_by_competitor:
                bt.logging.info(
                    f'Issue #{issue_number} solved by competitor '
                    f'{solution_result.solver_hotkey[:8]}... '
                    f'(GitHub: {solution_result.solver_github_username})'
                )
                return solution_result

            if solution_result.is_solved:
                # Issue solved but not by a competitor (external solution)
                result.is_solved = True
                result.solved_at = solution_result.solved_at
                result.solving_pr_url = solution_result.solving_pr_url
                result.solver_github_username = solution_result.solver_github_username

        return result

    except Exception as e:
        bt.logging.error(f'Error detecting solution for issue #{issue_number}: {e}')
        result.error = str(e)
        return result


def _get_issue_data(repository_full_name: str, issue_number: int, token: str) -> Optional[Dict]:
    """
    Fetch issue data from GitHub API.

    Args:
        repository_full_name: Repository in "owner/repo" format
        issue_number: Issue number
        token: GitHub access token

    Returns:
        Issue data dict or None if failed
    """
    headers = make_headers(token)

    try:
        response = requests.get(
            f'{BASE_GITHUB_API_URL}/repos/{repository_full_name}/issues/{issue_number}',
            headers=headers,
            timeout=15,
        )

        if response.status_code == 200:
            return response.json()

        bt.logging.warning(
            f'Failed to fetch issue #{issue_number}: HTTP {response.status_code}'
        )
        return None

    except Exception as e:
        bt.logging.error(f'Error fetching issue data: {e}')
        return None


def _get_linked_prs(
    repository_full_name: str,
    issue_number: int,
    token: str,
) -> List[Dict]:
    """
    Get PRs linked to an issue using the GitHub GraphQL API.

    This finds PRs that "close" or "fix" the issue via GitHub's keyword detection.

    Args:
        repository_full_name: Repository in "owner/repo" format
        issue_number: Issue number
        token: GitHub access token

    Returns:
        List of PR data dicts
    """
    owner, repo = repository_full_name.split('/')

    query = """
    query($owner: String!, $repo: String!, $issue_number: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $issue_number) {
          timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT, CLOSED_EVENT]) {
            nodes {
              ... on CrossReferencedEvent {
                source {
                  ... on PullRequest {
                    number
                    state
                    merged
                    mergedAt
                    createdAt
                    url
                    author {
                      login
                    }
                    body
                  }
                }
              }
              ... on ConnectedEvent {
                subject {
                  ... on PullRequest {
                    number
                    state
                    merged
                    mergedAt
                    createdAt
                    url
                    author {
                      login
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    variables = {
        'owner': owner,
        'repo': repo,
        'issue_number': issue_number,
    }

    try:
        response = requests.post(
            f'{BASE_GITHUB_API_URL}/graphql',
            headers=headers,
            json={'query': query, 'variables': variables},
            timeout=30,
        )

        if response.status_code != 200:
            bt.logging.warning(f'GraphQL query failed: HTTP {response.status_code}')
            return []

        data = response.json()

        if 'errors' in data:
            bt.logging.warning(f'GraphQL errors: {data["errors"]}')
            return []

        timeline_items = (
            data.get('data', {})
            .get('repository', {})
            .get('issue', {})
            .get('timelineItems', {})
            .get('nodes', [])
        )

        prs = []
        seen_pr_numbers = set()

        for item in timeline_items:
            # Check CrossReferencedEvent
            if 'source' in item and item['source']:
                pr = item['source']
                if pr.get('number') and pr['number'] not in seen_pr_numbers:
                    prs.append(pr)
                    seen_pr_numbers.add(pr['number'])

            # Check ConnectedEvent
            if 'subject' in item and item['subject']:
                pr = item['subject']
                if pr.get('number') and pr['number'] not in seen_pr_numbers:
                    prs.append(pr)
                    seen_pr_numbers.add(pr['number'])

        return prs

    except Exception as e:
        bt.logging.error(f'Error fetching linked PRs: {e}')
        return []


def _build_username_mapping(
    github_tokens: Dict[str, str],
    competitor_hotkeys: List[str],
) -> Dict[str, str]:
    """
    Build mapping of GitHub username -> hotkey for competitors.

    Args:
        github_tokens: Dict mapping hotkey -> github_access_token
        competitor_hotkeys: List of competing miner hotkeys

    Returns:
        Dict mapping lowercase github_username -> hotkey
    """
    username_to_hotkey: Dict[str, str] = {}

    for hotkey in competitor_hotkeys:
        token = github_tokens.get(hotkey)
        if not token:
            continue

        username = get_github_username(token)
        if username:
            username_to_hotkey[username.lower()] = hotkey
            bt.logging.debug(f'Mapped {username} -> {hotkey[:8]}...')

    return username_to_hotkey


def _check_pr_solution(
    pr_data: Dict,
    repository_full_name: str,
    token: str,
    competitor_usernames: Dict[str, str],
    competition_start_time: datetime,
    submission_window_end: datetime,
) -> SolutionDetectionResult:
    """
    Check if a specific PR represents a valid solution.

    A PR is a valid competitor solution if:
    1. It is merged
    2. It was created AFTER competition_start_time
    3. It was merged AFTER submission_window_end (prevents pre-made solutions)
    4. The author is one of the competitors

    Args:
        pr_data: PR data from GraphQL
        repository_full_name: Repository name
        token: GitHub access token
        competitor_usernames: Dict mapping username -> hotkey
        competition_start_time: When competition started
        submission_window_end: When submission window closed

    Returns:
        SolutionDetectionResult with detection status
    """
    result = SolutionDetectionResult(
        issue_id=0,  # Will be set by caller
        is_solved=False,
        solved_by_competitor=False,
    )

    # Check if PR is merged
    if not pr_data.get('merged'):
        return result

    pr_url = pr_data.get('url', '')
    pr_number = pr_data.get('number', 0)
    author = pr_data.get('author', {})
    author_login = author.get('login', '') if author else ''

    # Parse timestamps
    merged_at_str = pr_data.get('mergedAt')
    created_at_str = pr_data.get('createdAt')

    if not merged_at_str:
        return result

    try:
        merged_at = datetime.fromisoformat(merged_at_str.rstrip('Z')).replace(tzinfo=timezone.utc)
        created_at = (
            datetime.fromisoformat(created_at_str.rstrip('Z')).replace(tzinfo=timezone.utc)
            if created_at_str
            else None
        )
    except (ValueError, AttributeError) as e:
        bt.logging.warning(f'Error parsing PR timestamps: {e}')
        return result

    # Mark as solved (regardless of who solved it)
    result.is_solved = True
    result.solved_at = merged_at
    result.solving_pr_url = pr_url
    result.solver_github_username = author_login

    # Check if author is a competitor
    author_login_lower = author_login.lower()
    if author_login_lower not in competitor_usernames:
        bt.logging.debug(f'PR #{pr_number} author {author_login} is not a competitor')
        return result

    # Validate timing: PR must be created during competition
    if created_at and created_at < competition_start_time:
        bt.logging.warning(
            f'PR #{pr_number} was created before competition started '
            f'(created: {created_at}, start: {competition_start_time})'
        )
        return result

    # Validate timing: PR must be merged after submission window
    # This prevents miners from having pre-made solutions
    if merged_at < submission_window_end:
        bt.logging.warning(
            f'PR #{pr_number} was merged before submission window ended '
            f'(merged: {merged_at}, window_end: {submission_window_end})'
        )
        return result

    # Valid competitor solution!
    result.solved_by_competitor = True
    result.solver_hotkey = competitor_usernames[author_login_lower]

    return result


def check_external_solution(
    repository_full_name: str,
    issue_number: int,
    token: str,
    competition_start_time: datetime,
) -> Optional[str]:
    """
    Check if an issue was solved by someone outside the competition.

    This is used to detect when an external developer solves the issue,
    which should trigger competition cancellation.

    Args:
        repository_full_name: Repository in "owner/repo" format
        issue_number: Issue number
        token: GitHub access token
        competition_start_time: When competition started

    Returns:
        GitHub username of external solver, or None if not externally solved
    """
    issue_data = _get_issue_data(repository_full_name, issue_number, token)
    if not issue_data or issue_data.get('state') != 'closed':
        return None

    linked_prs = _get_linked_prs(repository_full_name, issue_number, token)
    for pr_data in linked_prs:
        if not pr_data.get('merged'):
            continue

        merged_at_str = pr_data.get('mergedAt')
        if merged_at_str:
            try:
                merged_at = datetime.fromisoformat(merged_at_str.rstrip('Z')).replace(tzinfo=timezone.utc)
                if merged_at > competition_start_time:
                    author = pr_data.get('author', {})
                    return author.get('login') if author else None
            except (ValueError, AttributeError):
                pass

    return None
