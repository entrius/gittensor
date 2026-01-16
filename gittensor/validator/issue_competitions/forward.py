# The MIT License (MIT)
# Copyright 2025 Entrius

"""Main forward pass logic for issue competitions sub-mechanism."""

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Set

import bittensor as bt
import numpy as np

from gittensor.synapses import GitPatSynapse

from .constants import (
    BLOCK_TIME_SECONDS,
    ISSUE_COMPETITIONS_ENABLED,
    MAX_ISSUE_PREFERENCES,
)
from .contract_client import (
    CompetitionStatus,
    ContractCompetition,
    IssueCompetitionContractClient,
)
from .elo import CompetitionRecord, EloRating, get_elo_rankings
from .pairing import find_pairs_for_issues
from .solution_detection import detect_issue_solution

if TYPE_CHECKING:
    from neurons.base.validator import BaseValidatorNeuron


async def get_rewards_for_issue_competitions(
    validator: 'BaseValidatorNeuron',
    miner_uids: List[int],
    contract_client: IssueCompetitionContractClient,
    elo_ratings: Dict[str, EloRating],
) -> np.ndarray:
    """
    Main entry point for issue competition rewards.

    This function orchestrates the issue competition sub-mechanism:
    1. Queries miners for issue preferences via synapse
    2. Processes preferences and finds pairs using ELO-priority matching
    3. Proposes miner pairs for issues on-chain
    4. Checks for solutions on active competitions
    5. Votes on winners or timeouts as needed

    Note: Actual rewards come from bounty payouts via the smart contract.
    This function returns zeros for the standard reward flow.

    Args:
        validator: The validator instance
        miner_uids: List of miner UIDs to evaluate
        contract_client: Client for smart contract interaction
        elo_ratings: Pre-calculated ELO ratings for miners

    Returns:
        np.ndarray of zeros (rewards come from on-chain bounties)
    """
    # Initialize zero rewards - issue competition rewards come from bounty payouts
    rewards = np.zeros(len(miner_uids))

    # Check if feature is enabled
    if not ISSUE_COMPETITIONS_ENABLED:
        bt.logging.debug('Issue competitions disabled, skipping')
        return rewards

    # Check if contract is configured
    if not contract_client.contract_address:
        bt.logging.warning('Issue competition contract not configured')
        return rewards

    try:
        # Step 1: Query miners for preferences and GitHub tokens
        bt.logging.info('Querying miners for issue preferences...')
        miner_responses = await _query_miners_for_preferences(validator, miner_uids)

        # Build preference and token maps
        miner_preferences: Dict[str, List[int]] = {}
        github_tokens: Dict[str, str] = {}

        for uid, response in miner_responses.items():
            if response and response.github_access_token:
                hotkey = validator.metagraph.hotkeys[uid]
                github_tokens[hotkey] = response.github_access_token

                # Get preferences (limit to MAX_ISSUE_PREFERENCES)
                preferences = getattr(response, 'issue_preferences', [])
                if preferences:
                    miner_preferences[hotkey] = preferences[:MAX_ISSUE_PREFERENCES]

        bt.logging.info(
            f'Received preferences from {len(miner_preferences)} miners, '
            f'{len(github_tokens)} tokens'
        )

        # Step 2: Get available issues from contract
        available_issues = contract_client.get_available_issues()
        bt.logging.info(f'Found {len(available_issues)} available issues')

        if not available_issues:
            bt.logging.debug('No available issues for competition')
        elif miner_preferences:
            # Step 3: Find pairs for new competitions
            await _process_new_pairings(
                validator,
                contract_client,
                available_issues,
                miner_preferences,
                elo_ratings,
            )

        # Step 4: Check active competitions for solutions
        active_competitions = contract_client.get_active_competitions()
        bt.logging.info(f'Found {len(active_competitions)} active competitions')

        if active_competitions:
            await _process_active_competitions(
                validator,
                contract_client,
                active_competitions,
                github_tokens,
            )

    except Exception as e:
        bt.logging.error(f'Error in issue competitions forward: {e}')

    return rewards


async def _query_miners_for_preferences(
    validator: 'BaseValidatorNeuron',
    miner_uids: List[int],
) -> Dict[int, Optional[GitPatSynapse]]:
    """
    Query miners for their issue preferences via synapse.

    Args:
        validator: The validator instance
        miner_uids: List of miner UIDs

    Returns:
        Dict mapping uid -> GitPatSynapse response
    """
    responses: Dict[int, Optional[GitPatSynapse]] = {}

    for uid in miner_uids:
        try:
            bt.logging.debug(f'Querying UID {uid} for issue preferences')

            response = await validator.dendrite(
                axons=[validator.metagraph.axons[uid]],
                synapse=GitPatSynapse(),
                deserialize=False,
            )

            responses[uid] = response[0] if response else None

        except Exception as e:
            bt.logging.debug(f'Error querying UID {uid}: {e}')
            responses[uid] = None

    return responses


async def _process_new_pairings(
    validator: 'BaseValidatorNeuron',
    contract_client: IssueCompetitionContractClient,
    available_issues: List,
    miner_preferences: Dict[str, List[int]],
    elo_ratings: Dict[str, EloRating],
) -> None:
    """
    Process pairing proposals for new competitions.

    Args:
        validator: The validator instance
        contract_client: Smart contract client
        available_issues: List of available issues
        miner_preferences: Hotkey -> preference list mapping
        elo_ratings: Hotkey -> EloRating mapping
    """
    # Get set of miners already in competition
    miners_in_competition: Set[str] = set()
    for hotkey in miner_preferences.keys():
        if contract_client.is_miner_in_competition(hotkey):
            miners_in_competition.add(hotkey)

    bt.logging.info(f'{len(miners_in_competition)} miners already in competition')

    # Find pairs using ELO-priority matching
    pairs = find_pairs_for_issues(
        available_issues,
        miner_preferences,
        elo_ratings,
        miners_in_competition,
    )

    if not pairs:
        bt.logging.debug('No pairs found for available issues')
        return

    # Propose pairs on-chain
    for issue_id, miner1, miner2 in pairs:
        try:
            # Check if there's already a proposal for this issue
            existing_proposal = contract_client.get_pair_proposal(issue_id)

            if existing_proposal:
                # Vote on existing proposal if it matches
                if (
                    existing_proposal.miner1_hotkey == miner1
                    and existing_proposal.miner2_hotkey == miner2
                ):
                    bt.logging.info(f'Voting on existing proposal for issue {issue_id}')
                    contract_client.vote_pair(issue_id, validator.wallet)
                else:
                    bt.logging.debug(
                        f'Different proposal exists for issue {issue_id}, skipping'
                    )
            else:
                # Create new proposal
                bt.logging.info(
                    f'Proposing pair for issue {issue_id}: '
                    f'{miner1[:8]}... vs {miner2[:8]}...'
                )
                contract_client.propose_pair(
                    issue_id, miner1, miner2, validator.wallet
                )

        except Exception as e:
            bt.logging.error(f'Error proposing pair for issue {issue_id}: {e}')


async def _process_active_competitions(
    validator: 'BaseValidatorNeuron',
    contract_client: IssueCompetitionContractClient,
    active_competitions: List[ContractCompetition],
    github_tokens: Dict[str, str],
) -> None:
    """
    Process active competitions - check for solutions and timeouts.

    Args:
        validator: The validator instance
        contract_client: Smart contract client
        active_competitions: List of active competitions
        github_tokens: Hotkey -> GitHub token mapping
    """
    current_block = _get_current_block(validator)

    for competition in active_competitions:
        try:
            await _process_single_competition(
                validator,
                contract_client,
                competition,
                github_tokens,
                current_block,
            )
        except Exception as e:
            bt.logging.error(
                f'Error processing competition {competition.id}: {e}'
            )


async def _process_single_competition(
    validator: 'BaseValidatorNeuron',
    contract_client: IssueCompetitionContractClient,
    competition: ContractCompetition,
    github_tokens: Dict[str, str],
    current_block: int,
) -> None:
    """
    Process a single active competition.

    Args:
        validator: The validator instance
        contract_client: Smart contract client
        competition: The competition to process
        github_tokens: Hotkey -> GitHub token mapping
        current_block: Current block number
    """
    # Get issue data for this competition
    issue = contract_client.get_issue(competition.issue_id)
    if not issue:
        bt.logging.warning(
            f'Issue {competition.issue_id} not found for competition {competition.id}'
        )
        return

    # Check if past deadline
    if current_block > competition.deadline_block:
        bt.logging.info(
            f'Competition {competition.id} past deadline, voting timeout'
        )
        contract_client.vote_timeout(competition.id, validator.wallet)
        return

    # Only check solutions after submission window ends
    if current_block <= competition.submission_window_end_block:
        bt.logging.debug(
            f'Competition {competition.id} still in submission window'
        )
        return

    # Check for solution
    competitor_hotkeys = [competition.miner1_hotkey, competition.miner2_hotkey]

    # Calculate timestamps from blocks
    competition_start_time = _blocks_to_datetime(
        competition.start_block, current_block
    )
    submission_window_end = _blocks_to_datetime(
        competition.submission_window_end_block, current_block
    )

    result = detect_issue_solution(
        github_tokens=github_tokens,
        repository_full_name=issue.repository_full_name,
        issue_number=issue.issue_number,
        competitor_hotkeys=competitor_hotkeys,
        competition_start_time=competition_start_time,
        submission_window_end=submission_window_end,
    )

    if result.error:
        bt.logging.warning(
            f'Solution detection error for competition {competition.id}: {result.error}'
        )
        return

    if result.solved_by_competitor and result.solver_hotkey and result.solving_pr_url:
        # Vote for winner
        bt.logging.info(
            f'Competition {competition.id} solved by {result.solver_hotkey[:8]}..., '
            f'voting for winner'
        )
        contract_client.vote_solution(
            competition.id,
            result.solver_hotkey,
            result.solving_pr_url,
            validator.wallet,
        )

    elif result.is_solved and not result.solved_by_competitor:
        # External solution - vote to cancel
        bt.logging.info(
            f'Competition {competition.id} solved externally by '
            f'{result.solver_github_username}, voting cancel'
        )
        contract_client.vote_cancel(
            competition.id,
            f'External solution by {result.solver_github_username}',
            validator.wallet,
        )


def _get_current_block(validator: 'BaseValidatorNeuron') -> int:
    """Get current block number."""
    try:
        return validator.subtensor.get_current_block()
    except Exception:
        return 0


def _blocks_to_datetime(block_number: int, current_block: int) -> datetime:
    """
    Convert a block number to approximate datetime.

    Args:
        block_number: The block number to convert
        current_block: Current block number

    Returns:
        Approximate datetime for the block
    """
    now = datetime.now(timezone.utc)
    blocks_diff = current_block - block_number
    seconds_diff = blocks_diff * BLOCK_TIME_SECONDS
    return now - timedelta(seconds=seconds_diff)


def get_elo_ratings_for_miners(
    validator: 'BaseValidatorNeuron',
    miner_uids: List[int],
    contract_client: Optional[IssueCompetitionContractClient] = None,
) -> Dict[str, EloRating]:
    """
    Get ELO ratings for all miners from stored competition history.

    This function retrieves competition records from storage (if available)
    and calculates ELO ratings using the EMA algorithm.

    Args:
        validator: The validator instance
        miner_uids: List of miner UIDs
        contract_client: Optional contract client for fetching competition history

    Returns:
        Dict mapping hotkey -> EloRating
    """
    elo_ratings: Dict[str, EloRating] = {}

    # Build miner info mapping
    miner_info: Dict[str, Dict] = {}
    for uid in miner_uids:
        hotkey = validator.metagraph.hotkeys[uid]
        miner_info[hotkey] = {'uid': uid}

    # Try to load competition history from database
    miner_competitions: Dict[str, List[CompetitionRecord]] = {}

    if hasattr(validator, 'db') and validator.db:
        try:
            # Attempt to load from database
            # TODO: Implement database storage for competition records
            pass
        except Exception as e:
            bt.logging.debug(f'Could not load competition history: {e}')

    # Calculate ELO ratings
    if miner_competitions:
        elo_ratings_list = get_elo_rankings(miner_competitions, miner_info)
        elo_ratings = {r.hotkey: r for r in elo_ratings_list}
    else:
        # No history - all miners get default ELO
        for uid in miner_uids:
            hotkey = validator.metagraph.hotkeys[uid]
            elo_ratings[hotkey] = EloRating(uid=uid, hotkey=hotkey)

    return elo_ratings
