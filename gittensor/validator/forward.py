# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from typing import TYPE_CHECKING

import bittensor as bt

from gittensor.validator.utils.load_weights import (
    load_master_repo_weights,
    load_programming_language_weights,
    load_token_config,
)

# ADD THIS for proper type hinting to navigate code easier.
if TYPE_CHECKING:
    from neurons.base.validator import BaseValidatorNeuron

from gittensor.utils.uids import get_all_uids
from gittensor.validator.evaluation.reward import get_rewards
from gittensor.validator.utils.config import VALIDATOR_STEPS_INTERVAL, VALIDATOR_WAIT

# Issue bounties integration
from gittensor.validator.issue_competitions import (
    ISSUE_BOUNTIES_ENABLED,
    ISSUES_CONTRACT_UID,
    forward_issue_bounties,
    IssueCompetitionContractClient,
    get_contract_address,
)


async def forward(self: 'BaseValidatorNeuron') -> None:
    """Execute the validator's forward pass.

    Performs the core validation cycle every VALIDATOR_STEPS_INTERVAL steps:
    1. Get all available miner UIDs
    2. Query miners and calculate rewards
    3. Update scores using exponential moving average

    Args:
        self: The validator instance containing all necessary state
    """

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        miner_uids = get_all_uids(self)

        master_repositories = load_master_repo_weights()
        programming_languages = load_programming_language_weights()
        token_config = load_token_config()

        # Count languages with tree-sitter support
        tree_sitter_count = sum(1 for c in token_config.language_configs.values() if c.language is not None)

        bt.logging.info('***** Starting scoring round *****')
        bt.logging.info(f'Total Repositories loaded from master_repositories.json: {len(master_repositories)}')
        bt.logging.info(f'Total Languages loaded from programming_languages.json: {len(programming_languages)}')
        bt.logging.info(f'Total Token config loaded from token_weights.json: {tree_sitter_count} tree-sitter languages')
        bt.logging.info(f'Number of neurons to evaluate: {len(miner_uids)}')

        # Get rewards for the responses - queries miners individually
        rewards = await get_rewards(self, miner_uids, master_repositories, programming_languages, token_config)

        # TODO: Remove this, hardcoded for testing to route emissions to contract treasury
        # rewards array is indexed by position in sorted(miner_uids), not by UID directly
        if ISSUES_CONTRACT_UID >= 0 and ISSUES_CONTRACT_UID in miner_uids:
            sorted_uids = sorted(miner_uids)
            idx = sorted_uids.index(ISSUES_CONTRACT_UID)
            rewards[0] = 0.01 # TODO: Remove - recycle uid gets little so emissions flow fast to contract for testing.
            rewards[idx] = 0.99
            bt.logging.info(f"Set reward for treasury UID {ISSUES_CONTRACT_UID} (index {idx}) to 0.5")

        # Update the scores based on the rewards
        self.update_scores(rewards, miner_uids)

        # =================================================================
        # Issue Bounties Sub-Mechanism
        # =================================================================
        if ISSUE_BOUNTIES_ENABLED:
            await _run_issue_bounties_forward(self)

    await asyncio.sleep(VALIDATOR_WAIT)


async def _run_issue_bounties_forward(validator: 'BaseValidatorNeuron') -> None:
    """
    Run the issue bounties forward pass.

    Checks active issues from the smart contract and votes on solutions
    when GitHub issues are closed by registered miners.
    """
    try:
        contract_addr = get_contract_address()
        if not contract_addr:
            bt.logging.debug("Issue bounties enabled but no contract address configured")
            return

        bt.logging.info(f"Running issue bounties forward (contract: {contract_addr[:12]}...)")

        # Create contract client
        contract_client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=validator.subtensor,
        )

        # Get miner GitHub mapping from validator's miner data
        # TODO: Populate from API or database - maps github_user_id -> miner_hotkey
        miners_github_mapping = getattr(validator, 'miners_github_mapping', {})

        # Get tier data from validator's scoring results
        # TODO: Populate from scoring results - maps hotkey -> tier info
        tier_data = getattr(validator, 'miner_tier_data', {})

        # Run issue bounties forward
        results = await forward_issue_bounties(
            validator=validator,
            contract_client=contract_client,
            miners_github_mapping=miners_github_mapping,
            tier_data=tier_data,
        )

        if results['votes_cast'] > 0 or results['cancels_cast'] > 0:
            bt.logging.success(
                f"Issue bounties: processed {results['issues_processed']} issues, "
                f"{results['votes_cast']} solution votes, {results['cancels_cast']} cancel votes"
            )
        elif results['issues_processed'] > 0:
            bt.logging.debug(f"Issue bounties: processed {results['issues_processed']} issues (no state changes)")

        if results['errors']:
            bt.logging.warning(f"Issue bounties errors: {results['errors'][:3]}")

    except Exception as e:
        bt.logging.error(f"Issue bounties forward failed: {e}")
