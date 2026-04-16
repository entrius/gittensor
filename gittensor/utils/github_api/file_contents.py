# Entrius 2025
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional

import bittensor as bt

from gittensor.constants import MAX_FILE_SIZE_BYTES, MAX_FILES_PER_GRAPHQL_BATCH

from . import graphql as gh_graphql

if TYPE_CHECKING:
    from gittensor.classes import FileChange as FileChangeType


def _fetch_file_contents_batch(
    repo_owner: str,
    repo_name: str,
    head_sha: str,
    batch_paths: List[str],
    token: str,
) -> Dict[str, Optional[str]]:
    """Fetch file contents for a single batch of paths in one GraphQL request.

    Args:
        repo_owner: Repository owner
        repo_name: Repository name
        head_sha: The commit SHA to fetch files at
        batch_paths: File paths for this batch
        token: GitHub PAT for authentication

    Returns:
        Dict mapping file paths to their contents (None if binary, deleted, or too large)
    """
    file_fields = []
    for i, path in enumerate(batch_paths):
        expression = f'{head_sha}:{path}'
        file_fields.append(
            f'file{i}: object(expression: "{expression}") {{ ... on Blob {{ text byteSize isBinary }} }}'
        )

    query = f"""
        query($owner: String!, $name: String!) {{
            repository(owner: $owner, name: $name) {{
                {' '.join(file_fields)}
            }}
        }}
    """

    variables = {'owner': repo_owner, 'name': repo_name}

    data = gh_graphql.execute_graphql_query(query, variables, token)
    if data is None:
        bt.logging.warning(f'Failed to fetch file contents for {repo_owner}/{repo_name}')
        return {path: None for path in batch_paths}

    if 'errors' in data:
        bt.logging.warning(f'GraphQL errors fetching files: {data["errors"]}')

    repo_data = data.get('data', {}).get('repository', {})
    results: Dict[str, Optional[str]] = {}

    for i, path in enumerate(batch_paths):
        file_data = repo_data.get(f'file{i}')

        if file_data is None:
            results[path] = None
        elif file_data.get('isBinary'):
            results[path] = None
        elif file_data.get('byteSize', 0) > MAX_FILE_SIZE_BYTES:
            results[path] = None
        else:
            results[path] = file_data.get('text')

    return results


def fetch_file_contents_batch(
    repo_owner: str,
    repo_name: str,
    head_sha: str,
    file_paths: List[str],
    token: str,
) -> Dict[str, Optional[str]]:
    """Fetch file contents in batched GraphQL requests so large PRs don't hit complexity limits.

    Args:
        repo_owner: Repository owner
        repo_name: Repository name
        head_sha: The commit SHA to fetch files at
        file_paths: List of file paths to fetch
        token: GitHub PAT for authentication

    Returns:
        Dict mapping file paths to their contents (None if binary, deleted, or too large)
    """
    if not file_paths:
        return {}

    results: Dict[str, Optional[str]] = {}

    for batch_start in range(0, len(file_paths), MAX_FILES_PER_GRAPHQL_BATCH):
        batch_paths = file_paths[batch_start : batch_start + MAX_FILES_PER_GRAPHQL_BATCH]
        batch_results = _fetch_file_contents_batch(repo_owner, repo_name, head_sha, batch_paths, token)
        results.update(batch_results)

    return results


@dataclass
class FileContentPair:
    """Holds both old (base) and new (head) content for a file."""

    old_content: Optional[str]  # None for new files
    new_content: Optional[str]  # None for deleted files


def _fetch_file_contents_with_base_batch(
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    head_sha: str,
    batch_changes: List['FileChangeType'],
    token: str,
) -> Dict[str, FileContentPair]:
    """Fetch base and head file contents for a single batch of file changes.

    Args:
        repo_owner: Repository owner
        repo_name: Repository name
        base_sha: The base branch SHA (before PR changes)
        head_sha: The head/merge commit SHA (after PR changes)
        batch_changes: File changes for this batch
        token: GitHub PAT for authentication

    Returns:
        Dict mapping file paths to FileContentPair (old_content, new_content)
    """
    file_fields = []
    for i, fc in enumerate(batch_changes):
        # Renames need the old path for the base version
        base_path = fc.previous_filename if fc.previous_filename else fc.filename
        head_path = fc.filename

        # New files have no base version to fetch
        if fc.status != 'added':
            base_expr = f'{base_sha}:{base_path}'
            file_fields.append(
                f'base{i}: object(expression: "{base_expr}") {{ ... on Blob {{ text byteSize isBinary }} }}'
            )

        # Deleted files have no head version to fetch
        if fc.status != 'removed':
            head_expr = f'{head_sha}:{head_path}'
            file_fields.append(
                f'head{i}: object(expression: "{head_expr}") {{ ... on Blob {{ text byteSize isBinary }} }}'
            )

    if not file_fields:
        return {}

    query = f"""
        query($owner: String!, $name: String!) {{
            repository(owner: $owner, name: $name) {{
                {' '.join(file_fields)}
            }}
        }}
    """

    variables = {'owner': repo_owner, 'name': repo_name}

    data = gh_graphql.execute_graphql_query(query, variables, token)
    if data is None:
        bt.logging.warning(f'Failed to fetch file contents for {repo_owner}/{repo_name}')
        return {fc.filename: FileContentPair(None, None) for fc in batch_changes}

    if 'errors' in data:
        bt.logging.warning(f'GraphQL errors fetching files: {data["errors"]}')

    repo_data = data.get('data', {}).get('repository', {})
    results: Dict[str, FileContentPair] = {}

    for i, fc in enumerate(batch_changes):
        old_content = None
        new_content = None

        # Pull the old content unless this file was just added
        if fc.status != 'added':
            base_data = repo_data.get(f'base{i}')
            if base_data and not base_data.get('isBinary') and base_data.get('byteSize', 0) <= MAX_FILE_SIZE_BYTES:
                old_content = base_data.get('text')

        # Pull the new content unless this file was removed
        if fc.status != 'removed':
            head_data = repo_data.get(f'head{i}')
            if head_data and not head_data.get('isBinary') and head_data.get('byteSize', 0) <= MAX_FILE_SIZE_BYTES:
                new_content = head_data.get('text')

        results[fc.filename] = FileContentPair(old_content=old_content, new_content=new_content)

    return results


def fetch_file_contents_with_base(
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    head_sha: str,
    file_changes: List['FileChangeType'],
    token: str,
) -> Dict[str, FileContentPair]:
    """Fetch old and new versions of files in batches so large PRs don't hit complexity limits.

    Args:
        repo_owner: Repository owner
        repo_name: Repository name
        base_sha: The base branch SHA (before PR changes)
        head_sha: The head/merge commit SHA (after PR changes)
        file_changes: List of FileChange objects (needed for status and previous_filename)
        token: GitHub PAT for authentication

    Returns:
        Dict mapping file paths to FileContentPair (old_content, new_content)
    """
    if not file_changes:
        return {}

    results: Dict[str, FileContentPair] = {}

    for batch_start in range(0, len(file_changes), MAX_FILES_PER_GRAPHQL_BATCH):
        batch = file_changes[batch_start : batch_start + MAX_FILES_PER_GRAPHQL_BATCH]
        batch_results = _fetch_file_contents_with_base_batch(repo_owner, repo_name, base_sha, head_sha, batch, token)
        results.update(batch_results)

    return results
