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
from gittensor.validator.evaluation.reward import get_rewards as get_rewards_for_oss
from gittensor.validator.issue_competitions import (
    IssueCompetitionContractClient,
    get_elo_ratings_for_miners,
    get_rewards_for_issue_competitions,
)
from gittensor.validator.issue_competitions.constants import (
    ISSUE_COMPETITIONS_ENABLED,
    ISSUE_CONTRACT_ADDRESS_TESTNET,
    ISSUES_CONTRACT_UID,
    ISSUES_EMISSION_WEIGHT,
    ISSUES_FIXED_EMISSION_RATE,
    OSS_EMISSION_WEIGHT,
)
from gittensor.validator.utils.config import VALIDATOR_STEPS_INTERVAL, VALIDATOR_WAIT


async def forward(self: 'BaseValidatorNeuron') -> None:
    """Execute the validator's forward pass (sub-subnet mechanism).

    Performs the core validation cycle every VALIDATOR_STEPS_INTERVAL steps.
    BOTH sub-mechanisms run at the SAME interval:
    1. OSS Contributions (existing) -> 50% of emissions
    2. Issue Competitions (new) -> 50% of emissions

    Args:
        self: The validator instance containing all necessary state
    """

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        miner_uids = get_all_uids(self)

        # =====================================================================
        # SUB-MECHANISM 1: OSS Contributions (50% emissions)
        # =====================================================================
        bt.logging.info('=' * 60)
        bt.logging.info('***** Starting OSS contributions scoring *****')
        bt.logging.info('=' * 60)

        master_repositories = load_master_repo_weights()
        programming_languages = load_programming_language_weights()
        token_config = load_token_config()

        # Count languages with tree-sitter support
        tree_sitter_count = sum(1 for c in token_config.language_configs.values() if c.language is not None)

        bt.logging.info(f'Total Repositories loaded from master_repositories.json: {len(master_repositories)}')
        bt.logging.info(f'Total Languages loaded from programming_languages.json: {len(programming_languages)}')
        bt.logging.info(f'Total Token config loaded from token_weights.json: {tree_sitter_count} tree-sitter languages')
        bt.logging.info(f'Number of neurons to evaluate: {len(miner_uids)}')

        # Get rewards for OSS contributions
        oss_rewards = await get_rewards_for_oss(
            self, miner_uids, master_repositories, programming_languages, token_config
        )

        # =====================================================================
        # SUB-MECHANISM 2: Issue Competitions (50% emissions)
        # =====================================================================
        bt.logging.info('=' * 60)
        bt.logging.info('***** Starting issue competitions scoring *****')
        bt.logging.info('=' * 60)

        if ISSUE_COMPETITIONS_ENABLED:
            # Initialize contract client
            contract_client = IssueCompetitionContractClient(
                contract_address=ISSUE_CONTRACT_ADDRESS_TESTNET,  # Switch to MAINNET for production
                subtensor=self.subtensor,
            )

            # Calculate ELO ratings from recent competitions
            elo_ratings = get_elo_ratings_for_miners(self, miner_uids, contract_client)

            # Get rewards for issue competitions
            # Note: Actual rewards come from bounty payouts, this returns zeros
            # but performs pairing, solution detection, and voting
            issues_rewards = await get_rewards_for_issue_competitions(
                self, miner_uids, contract_client, elo_ratings
            )
        else:
            bt.logging.info('Issue competitions DISABLED - skipping')
            # Return zeros when disabled
            import numpy as np
            issues_rewards = np.zeros(len(miner_uids))

        # =====================================================================
        # COMBINE: Weighted sum for final scores
        # =====================================================================
        bt.logging.info('=' * 60)
        bt.logging.info('***** Combining sub-mechanism rewards *****')
        bt.logging.info('=' * 60)

        # Combine rewards with emission weights
        # When issue competitions are disabled, full weight goes to OSS
        if ISSUE_COMPETITIONS_ENABLED:
            combined_rewards = (
                OSS_EMISSION_WEIGHT * oss_rewards +
                ISSUES_EMISSION_WEIGHT * issues_rewards
            )
            bt.logging.info(
                f'Combined rewards: OSS ({OSS_EMISSION_WEIGHT*100:.0f}%) + '
                f'Issues ({ISSUES_EMISSION_WEIGHT*100:.0f}%)'
            )
        else:
            combined_rewards = oss_rewards
            bt.logging.info('Using OSS rewards only (issues disabled)')

        # Update the scores based on the combined rewards
        self.update_scores(combined_rewards, miner_uids)

        # =====================================================================
        # CONTRACT UID EMISSIONS ROUTING (for issue bounty payouts)
        # =====================================================================
        if ISSUES_CONTRACT_UID >= 0 and ISSUES_FIXED_EMISSION_RATE > 0:
            bt.logging.info(f'Routing {ISSUES_FIXED_EMISSION_RATE:.2%} emissions to contract UID {ISSUES_CONTRACT_UID}')
            # Note: Actual emission routing is handled by setting weights
            # This log helps debug that the configuration is active

    await asyncio.sleep(VALIDATOR_WAIT)
