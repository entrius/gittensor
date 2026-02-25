# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from typing import TYPE_CHECKING, Dict

import bittensor as bt

from gittensor.classes import MinerEvaluation
from gittensor.constants import ISSUES_TREASURY_EMISSION_SHARE, ISSUES_TREASURY_UID
from gittensor.utils.uids import get_all_uids
from gittensor.validator.issue_competitions.forward import issue_competitions
from gittensor.validator.oss_contributions.reward import get_rewards
from gittensor.validator.utils.config import VALIDATOR_STEPS_INTERVAL, VALIDATOR_WAIT
from gittensor.validator.utils.load_weights import (
    load_master_repo_weights,
    load_programming_language_weights,
    load_token_config,
)

if TYPE_CHECKING:
    from neurons.base.validator import BaseValidatorNeuron


async def forward(self: 'BaseValidatorNeuron') -> None:
    """Execute the validator's forward pass.

    Performs the core validation cycle every VALIDATOR_STEPS_INTERVAL steps:
    1. Get all available miner UIDs
    2. Score OSS contributions and get miner evaluations
    3. Update scores using exponential moving average
    4. Run issue bounties verification (needs tier data from scoring)

    Args:
        self: The validator instance containing all necessary state
    """

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        miner_uids = get_all_uids(self)

        # Score OSS contributions - returns evaluations for issue verification
        miner_evaluations = await oss_contributions(self, miner_uids)

        # Issue bounties verification
        await issue_competitions(self, miner_evaluations)

    await asyncio.sleep(VALIDATOR_WAIT)


async def oss_contributions(self: 'BaseValidatorNeuron', miner_uids: set[int]) -> Dict[int, MinerEvaluation]:
    """Score OSS contributions and return miner evaluations for downstream use."""
    master_repositories = load_master_repo_weights()
    programming_languages = load_programming_language_weights()
    token_config = load_token_config()

    tree_sitter_count = sum(1 for c in token_config.language_configs.values() if c.language is not None)

    bt.logging.info('***** Starting scoring round *****')
    bt.logging.info(f'Total Repositories loaded: {len(master_repositories)}')
    bt.logging.info(f'Total Languages loaded: {len(programming_languages)}')
    bt.logging.info(f'Token config: {tree_sitter_count} tree-sitter languages')
    bt.logging.info(f'Neurons to evaluate: {len(miner_uids)}')

    rewards, miner_evaluations = await get_rewards(
        self, miner_uids, master_repositories, programming_languages, token_config
    )

    # -------------------------------------------------------------------------
    # Issue Bounties Treasury Allocation
    # The smart contract neuron (ISSUES_TREASURY_UID) accumulates emissions
    # which fund issue bounty payouts. We allocate a fixed percentage of
    # total emissions to this treasury by scaling down all miner rewards
    # and assigning the remainder to the treasury UID.
    # -------------------------------------------------------------------------
    if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in miner_uids:
        treasury_share = ISSUES_TREASURY_EMISSION_SHARE
        miner_share = 1.0 - treasury_share

        # rewards array is indexed by position in sorted(miner_uids)
        sorted_uids = sorted(miner_uids)
        treasury_idx = sorted_uids.index(ISSUES_TREASURY_UID)

        # Scale down all rewards proportionally
        rewards *= miner_share

        # Assign treasury's share
        rewards[treasury_idx] = treasury_share

        bt.logging.info(
            f'Treasury allocation: Smart Contract UID {ISSUES_TREASURY_UID} receives '
            f'{treasury_share * 100:.0f}% of emissions, miners share {miner_share * 100:.0f}%'
        )

    self.update_scores(rewards, miner_uids)

    return miner_evaluations
