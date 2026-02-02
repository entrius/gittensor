# The MIT License (MIT)
# Copyright 2025 Entrius

"""
Forward pass for Issue Bounties sub-mechanism

1. Continue running OSS contribution scoring (to know miner tiers)
2. Get active issues from smart contract
3. For each active issue:
   - Query GitHub API to check if issue is CLOSED
   - If solved by bronze+ miner -> vote_solution(issue_id, solver_hotkey, pr_url)
   - If closed but not by miner -> vote_cancel_issue(issue_id)
"""

import asyncio
from typing import Dict, List, Optional, Any

import bittensor as bt

from .contract_client import (
    IssueCompetitionContractClient,
    IssueStatus,
    ContractIssue,
)


async def check_github_issue_closed(issue: ContractIssue) -> Optional[Dict[str, Any]]:
    """
    Check if a GitHub issue is closed and get the solving PR info.

    Args:
        issue: The contract issue to check

    Returns:
        Dict with 'is_closed', 'solver_github_id', 'pr_url' or None on error
    """
    try:
        import aiohttp

        repo = issue.repository_full_name
        issue_num = issue.issue_number

        # Query GitHub API
        url = f"https://api.github.com/repos/{repo}/issues/{issue_num}"
        headers = {"Accept": "application/vnd.github.v3+json"}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    bt.logging.warning(f"GitHub API error for {repo}#{issue_num}: {response.status}")
                    return None

                data = await response.json()

                is_closed = data.get('state') == 'closed'

                if not is_closed:
                    return {'is_closed': False}

                # Find linked PR that closed the issue
                # Check closed_by field or search linked PRs
                solver_github_id = None
                pr_url = None

                # If issue has pull_request field, it was closed by a PR
                if data.get('pull_request'):
                    pr_url = data['pull_request'].get('html_url')

                # Get the user who closed it (approximation)
                if data.get('closed_by'):
                    solver_github_id = data['closed_by'].get('id')

                return {
                    'is_closed': True,
                    'solver_github_id': solver_github_id,
                    'pr_url': pr_url,
                }

    except ImportError:
        bt.logging.warning("aiohttp not available - cannot check GitHub")
        return None
    except Exception as e:
        bt.logging.error(f"Error checking GitHub issue: {e}")
        return None


def lookup_miner_by_github_id(github_id: int, miners_data: Dict) -> Optional[str]:
    """
    Look up miner hotkey by GitHub user ID.

    Args:
        github_id: GitHub user ID
        miners_data: Dict mapping github_id -> miner_hotkey

    Returns:
        Miner hotkey or None if not found
    """
    return miners_data.get(str(github_id))


def get_miner_coldkey(hotkey: str, subtensor: bt.Subtensor, netuid: int) -> Optional[str]:
    """
    Get the coldkey for a miner's hotkey.

    Args:
        hotkey: Miner's hotkey address
        subtensor: Bittensor subtensor instance
        netuid: Network UID

    Returns:
        Coldkey address or None
    """
    try:
        # Query on-chain for coldkey associated with hotkey
        result = subtensor.query_subtensor("Owner", None, [hotkey])
        if result:
            return str(result)
    except Exception as e:
        bt.logging.debug(f"Error getting coldkey for {hotkey}: {e}")
    return None


def is_bronze_or_higher(hotkey: str, tier_data: Dict) -> bool:
    """
    Check if a miner is bronze tier or higher.

    Bronze tier requirements (from tier_config.py):
    - 70% credibility (merged/attempted ratio)
    - 3+ unique repositories
    - 5+ token score per repository

    Args:
        hotkey: Miner hotkey
        tier_data: Dict with miner tier information

    Returns:
        True if miner is bronze+ tier
    """
    miner_tier = tier_data.get(hotkey, {})

    # Check credibility
    credibility = miner_tier.get('credibility', 0)
    if credibility < 0.7:
        return False

    # Check unique repos
    unique_repos = miner_tier.get('unique_repos', 0)
    if unique_repos < 3:
        return False

    # Check token score
    avg_score = miner_tier.get('avg_token_score', 0)
    if avg_score < 5:
        return False

    return True


async def forward_issue_bounties(
    validator,
    contract_client: IssueCompetitionContractClient,
    miners_github_mapping: Dict[str, str],
    tier_data: Dict[str, Dict],
) -> Dict[str, Any]:
    """
    1. Get active issues from smart contract
    2. For each active issue:
       - Query GitHub API to check if issue is CLOSED
       - If solved by bronze+ miner -> vote_solution(issue_id, solver_hotkey, pr_url)
       - If closed but not by miner -> vote_cancel_issue(issue_id)

    Args:
        validator: Validator instance with wallet
        contract_client: Contract client instance
        miners_github_mapping: Dict mapping github_id -> miner_hotkey
        tier_data: Dict with miner tier information

    Returns:
        Dict with results: votes_cast, issues_processed, errors
    """
    results = {
        'issues_processed': 0,
        'votes_cast': 0,
        'cancels_cast': 0,
        'errors': [],
    }

    try:
        # Get active issues from contract
        active_issues = contract_client.get_issues_by_status(IssueStatus.ACTIVE)
        bt.logging.info(f"Found {len(active_issues)} active issues")

        for issue in active_issues:
            results['issues_processed'] += 1

            try:
                # Check GitHub status
                github_state = await check_github_issue_closed(issue)

                if github_state is None:
                    bt.logging.warning(f"Could not check GitHub for issue {issue.id}")
                    continue

                if not github_state.get('is_closed'):
                    # Issue still open - skip
                    continue

                # Issue is closed - find solver
                solver_github_id = github_state.get('solver_github_id')
                pr_url = github_state.get('pr_url', '')

                if solver_github_id:
                    miner_hotkey = lookup_miner_by_github_id(solver_github_id, miners_github_mapping)

                    if miner_hotkey and is_bronze_or_higher(miner_hotkey, tier_data):
                        # Valid solver found - vote solution
                        miner_coldkey = get_miner_coldkey(
                            miner_hotkey,
                            validator.subtensor,
                            validator.config.netuid
                        )

                        if miner_coldkey:
                            success = contract_client.vote_solution(
                                issue_id=issue.id,
                                solver_hotkey=miner_hotkey,
                                solver_coldkey=miner_coldkey,
                                pr_url=pr_url or f"https://github.com/{issue.repository_full_name}/issues/{issue.issue_number}",
                                wallet=validator.wallet,
                            )

                            if success:
                                results['votes_cast'] += 1
                                bt.logging.success(
                                    f"Voted solution for issue {issue.id}: {miner_hotkey[:12]}..."
                                )
                            else:
                                results['errors'].append(f"Vote failed for issue {issue.id}")
                        else:
                            bt.logging.warning(f"Could not get coldkey for {miner_hotkey[:12]}...")
                    else:
                        # Closed but not by eligible miner - vote cancel
                        success = contract_client.vote_cancel_issue(
                            issue_id=issue.id,
                            reason="Issue closed externally (not by registered miner)",
                            wallet=validator.wallet,
                        )

                        if success:
                            results['cancels_cast'] += 1
                            bt.logging.info(f"Voted cancel for issue {issue.id} (external solution)")
                else:
                    # No solver found - vote cancel
                    success = contract_client.vote_cancel_issue(
                        issue_id=issue.id,
                        reason="Issue closed without identifiable solver",
                        wallet=validator.wallet,
                    )

                    if success:
                        results['cancels_cast'] += 1

            except Exception as e:
                bt.logging.error(f"Error processing issue {issue.id}: {e}")
                results['errors'].append(f"Issue {issue.id}: {str(e)}")

    except Exception as e:
        bt.logging.error(f"Forward loop error: {e}")
        results['errors'].append(str(e))

    return results
