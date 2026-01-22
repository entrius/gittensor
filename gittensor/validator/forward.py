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
    EmissionHarvester,
    IssueCompetitionContractClient,
    create_harvester_for_validator,
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

        # Check if issue competitions are enabled (constant or validator config)
        issue_competitions_enabled = ISSUE_COMPETITIONS_ENABLED
        if hasattr(self, 'config') and hasattr(self.config, 'issue_competitions'):
            config_enabled = getattr(self.config.issue_competitions, 'enabled', None)
            if config_enabled is not None:
                issue_competitions_enabled = config_enabled
                bt.logging.debug(f'Issue competitions enabled override from config: {config_enabled}')

        bt.logging.debug(f'Issue competitions status:')
        bt.logging.debug(f'  ISSUE_COMPETITIONS_ENABLED constant: {ISSUE_COMPETITIONS_ENABLED}')
        bt.logging.debug(f'  Final enabled value: {issue_competitions_enabled}')
        bt.logging.debug(f'  ISSUES_CONTRACT_UID: {ISSUES_CONTRACT_UID}')

        if issue_competitions_enabled:
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
            bt.logging.info('Issue competitions DISABLED - skipping scoring')
            bt.logging.debug('  To enable: set ISSUE_COMPETITIONS_ENABLED=true env var')
            bt.logging.debug('  Or add "issue_competitions_enabled": true to ~/.gittensor/contract_config.json')
            bt.logging.debug('  Or pass --issue_competitions.enabled true to validator')
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
        if issue_competitions_enabled:
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


        combined_rewards[0] = 0.25
        combined_rewards[1] = 0.25
        combined_rewards[ISSUES_CONTRACT_UID] = 0.5
        for n, reward in enumerate(combined_rewards):
            bt.logging.info(f'uid {n} = {reward} ')

        # Update the scores based on the combined rewards
        self.update_scores(combined_rewards, miner_uids)

    # =========================================================================
    # HARVEST: Check for and harvest emissions from contract treasury
    # This runs EVERY forward() call (not gated by VALIDATOR_STEPS_INTERVAL)
    # The harvester has its own internal interval (100 blocks) to control frequency
    # =========================================================================
    await _check_and_harvest_emissions(self)

    await asyncio.sleep(VALIDATOR_WAIT)


async def _check_and_harvest_emissions(self: 'BaseValidatorNeuron') -> None:
    """
    Check for and harvest emissions from contract treasury.

    This is called every forward() iteration but the harvester's internal
    interval logic controls when actual harvesting occurs (every 100 blocks).
    """
    # Check if issue competitions are enabled
    issue_competitions_enabled = ISSUE_COMPETITIONS_ENABLED
    if hasattr(self, 'config') and hasattr(self.config, 'issue_competitions'):
        config_enabled = getattr(self.config.issue_competitions, 'enabled', None)
        if config_enabled is not None:
            issue_competitions_enabled = config_enabled

    if not issue_competitions_enabled:
        return

    # Initialize harvester if not already done (first call only)
    if not hasattr(self, '_emission_harvester'):
        bt.logging.info('Initializing emission harvester...')
        self._emission_harvester = create_harvester_for_validator(self)
        if self._emission_harvester:
            bt.logging.info(f'Emission harvester initialized: enabled={self._emission_harvester.enabled}, '
                          f'interval={self._emission_harvester.harvest_config.interval_blocks} blocks')
        else:
            bt.logging.warning('Failed to create emission harvester')
            return

    if not self._emission_harvester:
        return

    try:
        current_block = self.subtensor.get_current_block()

        # Check if harvest is due (internal interval check)
        if not self._emission_harvester.should_harvest(current_block):
            return  # Not time yet, skip silently

        # Time to harvest - log and execute
        bt.logging.debug(f'Harvester check: block={current_block}, '
                        f'last_harvest={self._emission_harvester.last_harvest_block}')

        harvest_result = await self._emission_harvester.maybe_harvest(current_block)

        if harvest_result:
            if harvest_result.get('harvested', 0) > 0:
                bt.logging.success(f'Emission harvest completed: {harvest_result}')
            elif harvest_result.get('status') == 'no_pending':
                bt.logging.debug('Harvest check: no pending emissions')
            else:
                bt.logging.debug(f'Harvest result: {harvest_result}')

    except Exception as e:
        bt.logging.error(f'Emission harvest failed: {e}')
        import traceback
        bt.logging.debug(f'Harvest traceback: {traceback.format_exc()}')
