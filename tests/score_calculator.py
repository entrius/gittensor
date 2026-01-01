# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
PR Score Calculator Test Script

This script allows you to analyze a merged PR from any GitHub repository and calculate
its score using the gittensor scoring logic. It fetches the PR changes via GitHub API
and runs them through the calculate_score_from_file_changes function.

Usage:
    export GITHUB_PAT=your_github_personal_access_token
    python tests/test_pr_score_calculator.py <repository> <pr_number>

Example:
    python tests/test_pr_score_calculator.py owner/repo 123

The script will:
1. Fetch the PR details and file changes from GitHub
2. Calculate the base score using calculate_score_from_file_changes
3. Display a detailed report showing:
   - Score per file (with language weight and contribution)
   - Total PR score
   - PR metadata (additions, deletions, commits, etc.)
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import List, Optional

# Add the project root to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import bittensor as bt
import requests

# Import directly to avoid circular dependencies
from gittensor.classes import FileChange, PullRequest
from gittensor.constants import (
    BASE_GITHUB_API_URL,
    DEFAULT_PROGRAMMING_LANGUAGE_WEIGHT,
    MAX_LINES_SCORED_FOR_MITIGATED_EXT,
    MITIGATED_EXTENSIONS,
)


def make_headers(token: str):
    """Helper function for formatting headers for requests"""
    return {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}


def get_pull_request_file_changes(repository: str, pr_number: int, token: str) -> Optional[List[FileChange]]:
    """
    Get the diff for a specific PR by repository name and PR number

    Args:
        repository (str): Repository in format 'owner/repo'
        pr_number (int): PR number
        token (str): Github pat
    Returns:
        List[FileChanges]: List object with file changes or None if error
    """
    headers = make_headers(token)

    try:
        response = requests.get(
            f'{BASE_GITHUB_API_URL}/repos/{repository}/pulls/{pr_number}/files', headers=headers, timeout=15
        )
        if response.status_code == 200:
            file_diffs = response.json()
            return [FileChange.from_github_response(pr_number, repository, file_diff) for file_diff in file_diffs]

        return []

    except Exception as e:
        bt.logging.error(f'Error getting file changes for PR #{pr_number} in {repository}: {e}')
        return []


def load_programming_language_weights():
    """Load programming language weights from the weights directory"""
    import json

    weights_path = os.path.join(
        os.path.dirname(__file__), '..', 'gittensor', 'validator', 'weights', 'programming_languages.json'
    )

    try:
        with open(weights_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        bt.logging.error(f'Error loading programming language weights: {e}')
        return {}


def get_pr_details(repository: str, pr_number: int, token: str) -> dict:
    """
    Fetch PR details from GitHub API.

    Args:
        repository: Repository in format 'owner/repo'
        pr_number: PR number
        token: GitHub PAT

    Returns:
        dict: PR details from GitHub API
    """
    headers = make_headers(token)

    try:
        response = requests.get(
            f'{BASE_GITHUB_API_URL}/repos/{repository}/pulls/{pr_number}', headers=headers, timeout=15
        )

        if response.status_code == 200:
            return response.json()
        else:
            bt.logging.error(f'Failed to fetch PR details: {response.status_code} - {response.text}')
            return None

    except Exception as e:
        bt.logging.error(f'Error fetching PR details: {e}')
        return None


def calculate_file_score_breakdown(file_changes: list[FileChange], programming_languages: dict) -> list[dict]:
    """
    Calculate detailed score breakdown for each file.

    Args:
        file_changes: List of FileChange objects
        programming_languages: Dict of language weights

    Returns:
        list[dict]: List of file score breakdowns with details
    """
    total_file_changes = sum(fc.changes for fc in file_changes)
    file_breakdowns = []

    for file in file_changes:
        language_weight = programming_languages.get(file.file_extension, DEFAULT_PROGRAMMING_LANGUAGE_WEIGHT)
        actual_changes = file.changes

        # Cap scored changes for extensions that are exploitable
        scored_changes = actual_changes
        is_capped = False
        if file.file_extension in MITIGATED_EXTENSIONS:
            scored_changes = min(actual_changes, MAX_LINES_SCORED_FOR_MITIGATED_EXT)
            is_capped = actual_changes > MAX_LINES_SCORED_FOR_MITIGATED_EXT

        # Normalized by total changes in the PR
        weight_ratio = actual_changes / total_file_changes if total_file_changes > 0 else 0
        file_score = language_weight * weight_ratio * (scored_changes**0.75)

        file_breakdowns.append(
            {
                'filename': file.filename,
                'extension': file.file_extension or '(no extension)',
                'changes': actual_changes,
                'scored_changes': scored_changes,
                'is_capped': is_capped,
                'additions': file.additions,
                'deletions': file.deletions,
                'language_weight': language_weight,
                'weight_ratio': weight_ratio,
                'file_score': file_score,
                'status': file.status,
            }
        )

    return file_breakdowns


def print_report(pr_details: dict, file_changes: list[FileChange], programming_languages: dict):
    """
    Print a detailed report of the PR score calculation.

    Args:
        pr_details: PR details from GitHub API
        file_changes: List of FileChange objects
        programming_languages: Dict of language weights
    """
    # Create a mock PullRequest object to use calculate_score_from_file_changes
    pr = PullRequest(
        number=pr_details['number'],
        repository_full_name=pr_details['base']['repo']['full_name'],
        uid=0,  # Mock UID
        hotkey='test',  # Mock hotkey
        github_id='test',  # Mock github_id
        title=pr_details['title'],
        author_login=pr_details['user']['login'],
        merged_at=datetime.now(timezone.utc),  # Mock timestamp
        created_at=datetime.now(timezone.utc),  # Mock timestamp
        additions=pr_details['additions'],
        deletions=pr_details['deletions'],
        commits=pr_details['commits'],
        file_changes=file_changes,
    )

    # Calculate score using the actual function
    base_score = pr.calculate_score_from_file_changes(programming_languages)
    pr.base_score = base_score

    # For this calculator, earned_score equals base_score (no penalties/multipliers applied)
    # In the actual validator, earned_score would be modified by uniqueness multipliers, penalties, etc.
    earned_score = base_score
    pr.earned_score = earned_score

    # Get detailed breakdown
    file_breakdowns = calculate_file_score_breakdown(file_changes, programming_languages)

    # Sort by score descending
    file_breakdowns.sort(key=lambda x: x['file_score'], reverse=True)

    # Print report
    print('\n' + '=' * 100)
    print('PR SCORE CALCULATION REPORT')
    print('=' * 100)

    print(f'\nRepository: {pr_details["base"]["repo"]["full_name"]}')
    print(f'PR Number: #{pr_details["number"]}')
    print(f'Title: {pr_details["title"]}')
    print(f'Author: {pr_details["user"]["login"]}')
    print(f'State: {pr_details["state"]}')
    if pr_details.get('merged_at'):
        print(f'Merged At: {pr_details["merged_at"]}')
    print(f'\nTotal Additions: {pr_details["additions"]}')
    print(f'Total Deletions: {pr_details["deletions"]}')
    print(f'Total Changes: {pr_details["additions"] + pr_details["deletions"]}')
    print(f'Number of Commits: {pr_details["commits"]}')
    print(f'Number of Files Changed: {len(file_changes)}')
    print(f'\nBase Score: {pr.base_score:.6f}')
    print(f'Earned Score: {pr.earned_score:.6f}')
    score_difference = pr.earned_score - pr.base_score
    if abs(score_difference) > 0.000001:  # Check if there's a meaningful difference
        print(f'Score Difference: {score_difference:+.6f} ({(score_difference / pr.base_score) * 100:+.2f}%)')
    else:
        print('Score Difference: None (no penalties or multipliers applied)')

    print('\n' + '-' * 100)
    print('FILE-BY-FILE SCORE BREAKDOWN')
    print('-' * 100)
    print(f'{"Filename":<50} {"Ext":<8} {"Changes":<10} {"Lang Wt":<10} {"Contrib %":<12} {"Score":<12} {"Status":<10}')
    print('-' * 100)

    for fb in file_breakdowns:
        capped_indicator = ' (CAPPED)' if fb['is_capped'] else ''
        print(
            f'{fb["filename"]:<50} {fb["extension"]:<8} {fb["changes"]:<10} '
            f'{fb["language_weight"]:<10.4f} {fb["weight_ratio"] * 100:<12.2f} '
            f'{fb["file_score"]:<12.6f} {fb["status"]:<10}{capped_indicator}'
        )

    print('-' * 100)
    print(f'\n{"BASE SCORE (from file changes):":<80} {pr.base_score:.6f}')
    print(f'{"EARNED SCORE (after penalties/multipliers):":<80} {pr.earned_score:.6f}')
    score_diff = pr.earned_score - pr.base_score
    if abs(score_diff) > 0.000001:
        print(f'{"SCORE ADJUSTMENT:":<80} {score_diff:+.6f} ({(score_diff / pr.base_score) * 100:+.2f}%)')
    print(f'{"TOTAL LINES SCORED (after capping):":<80} {pr.total_lines_scored}')
    print('=' * 100 + '\n')

    # Additional insights
    if any(fb['is_capped'] for fb in file_breakdowns):
        print('\nNOTE: Some files have been capped at max scored changes due to exploit mitigation.')
        capped_files = [fb for fb in file_breakdowns if fb['is_capped']]
        print(f'Capped files ({len(capped_files)}):')
        for fb in capped_files:
            print(
                f'  - {fb["filename"]}: {fb["changes"]} changes -> {fb["scored_changes"]} scored (cap: {MAX_LINES_SCORED_FOR_MITIGATED_EXT})'
            )

    # Language distribution
    print('\nLANGUAGE DISTRIBUTION:')
    lang_stats = {}
    for fb in file_breakdowns:
        ext = fb['extension']
        if ext not in lang_stats:
            lang_stats[ext] = {'count': 0, 'changes': 0, 'score': 0}
        lang_stats[ext]['count'] += 1
        lang_stats[ext]['changes'] += fb['changes']
        lang_stats[ext]['score'] += fb['file_score']

    for ext, stats in sorted(lang_stats.items(), key=lambda x: x[1]['score'], reverse=True):
        print(f'  {ext:<15} Files: {stats["count"]:<3} Changes: {stats["changes"]:<8} Score: {stats["score"]:.6f}')


def main():
    """
    Main entry point for the PR score calculator.
    """
    parser = argparse.ArgumentParser(
        description='Calculate PR score using gittensor scoring logic',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tests/test_pr_score_calculator.py entrius/gittensor 22
  python tests/test_pr_score_calculator.py owner/repo 123

Environment Variables:
  GITHUB_PAT: GitHub Personal Access Token (required)
        """,
    )

    parser.add_argument('repository', help='Repository in format owner/repo')
    parser.add_argument('pr_number', type=int, help='PR number')

    args = parser.parse_args()

    # Get GitHub PAT from environment
    github_pat = os.environ.get('GITHUB_PAT')
    if not github_pat:
        bt.logging.error('GITHUB_PAT environment variable not set!')
        bt.logging.error('Please set it with: export GITHUB_PAT=your_token')
        sys.exit(1)

    bt.logging.info(f'Fetching PR #{args.pr_number} from {args.repository}...')

    # Fetch PR details
    pr_details = get_pr_details(args.repository, args.pr_number, github_pat)
    if not pr_details:
        bt.logging.error('Failed to fetch PR details. Please check the repository and PR number.')
        sys.exit(1)

    # Fetch file changes
    bt.logging.info('Fetching file changes...')
    file_changes = get_pull_request_file_changes(args.repository, args.pr_number, github_pat)
    if not file_changes:
        bt.logging.error('Failed to fetch file changes or PR has no file changes.')
        sys.exit(1)

    bt.logging.info(f'Found {len(file_changes)} file changes')

    # Load programming language weights
    bt.logging.info('Loading programming language weights...')
    programming_languages = load_programming_language_weights()

    # Generate and print report
    print_report(pr_details, file_changes, programming_languages)


if __name__ == '__main__':
    """
    Run the PR score calculator.

    Example usage:
        export GITHUB_PAT=ghp_xxxxxxxxxxxxxxxxxxxx
        python -m tests.pr_score_calculator entrius/gittensor 5
    """
    try:
        main()
    except KeyboardInterrupt:
        bt.logging.info('\nInterrupted by user')
        sys.exit(0)
    except Exception as e:
        bt.logging.error(f'Error: {e}')
        import traceback

        traceback.print_exc()
        sys.exit(1)
