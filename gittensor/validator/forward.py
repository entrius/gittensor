# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from typing import TYPE_CHECKING, Any, Dict

import bittensor as bt

from gittensor.validator.utils.load_weights import load_master_repo_weights, load_programming_language_weights

# ADD THIS for proper type hinting to navigate code easier.
if TYPE_CHECKING:
    from neurons.base.validator import BaseValidatorNeuron

from gittensor.utils.uids import get_all_uids
from gittensor.validator.evaluation.reward import get_rewards
from gittensor.validator.utils.config import VALIDATOR_STEPS_INTERVAL, VALIDATOR_WAIT


async def forward(self: "BaseValidatorNeuron") -> None:
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

        bt.logging.info("***** Starting scoring round *****")
        bt.logging.info(f"Total Repositories loaded from master_repositories.json: {len(master_repositories)}")
        bt.logging.info(f"Total Languages loaded from programming_languages.json: {len(programming_languages)}")
        bt.logging.info(f"Number of neurons to evaluate: {len(miner_uids)}")

        # Get rewards for the responses - queries miners individually
        rewards = await get_rewards(self, miner_uids, master_repositories, programming_languages)

        # Update the scores based on the rewards
        self.update_scores(rewards, miner_uids)

    await asyncio.sleep(VALIDATOR_WAIT)
