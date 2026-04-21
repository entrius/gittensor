# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared GitHub credential validation used by multiple validator subsystems."""

from typing import Optional, Tuple

import requests

from gittensor.constants import BASE_GITHUB_API_URL, GITHUB_HTTP_TIMEOUT_SECONDS
from gittensor.utils.github_api_tools import get_github_id
from gittensor.validator.utils.load_weights import load_master_repo_weights


def validate_github_credentials(uid: int, pat: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Validate PAT and return (github_id, error_reason) tuple."""
    if not pat:
        return None, f'No Github PAT provided by miner {uid}'

    github_id = get_github_id(pat)
    if not github_id:
        return None, f"No Github id found for miner {uid}'s PAT"

    return github_id, None


def validate_github_repo_access(pat: Optional[str]) -> Optional[str]:
    """Validate that a PAT can query at least one tracked repository via GraphQL."""
    if not pat:
        return 'No Github PAT provided'

    master_repositories = load_master_repo_weights()
    if not master_repositories:
        return None

    repo_name = next(iter(sorted(master_repositories)))
    try:
        owner, name = repo_name.split('/', 1)
    except ValueError:
        return f'Invalid tracked repository name: {repo_name}'

    query = """
        query($owner: String!, $name: String!) {
          repository(owner: $owner, name: $name) {
            id
          }
        }
    """
    headers = {'Authorization': f'Bearer {pat}', 'Accept': 'application/json'}

    try:
        response = requests.post(
            f'{BASE_GITHUB_API_URL}/graphql',
            json={'query': query, 'variables': {'owner': owner, 'name': name}},
            headers=headers,
            timeout=GITHUB_HTTP_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            return f'GitHub GraphQL API returned {response.status_code}'
        data = response.json()
        if 'errors' in data:
            return f'GraphQL error: {data["errors"][0].get("message", "unknown")}'
        if not data.get('data', {}).get('repository'):
            return f'PAT could not access tracked repo {repo_name}'
        return None
    except requests.RequestException as e:
        return str(e)
