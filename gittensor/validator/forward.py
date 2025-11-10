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
    import time

    bt.logging.debug(f"Forward pass step {self.step} (interval: {VALIDATOR_STEPS_INTERVAL})")

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        round_start_time = time.time()
        bt.logging.info("="*70)
        bt.logging.info("***** Starting scoring round *****")
        bt.logging.info(f"Current step: {self.step}")
        bt.logging.info(f"Block: {self.block}")
        bt.logging.info(f"Validator hotkey: {self.wallet.hotkey.ss58_address}")
        bt.logging.info("="*70)

        # Load configuration
        bt.logging.info("Loading repository and language weights...")
        master_repositories = load_master_repo_weights()
        programming_languages = load_programming_language_weights()
        bt.logging.info(f"✓ Loaded {len(master_repositories)} repositories")
        bt.logging.info(f"✓ Loaded {len(programming_languages)} programming languages")

        # Get miner UIDs
        bt.logging.info("Fetching active miner UIDs...")
        miner_uids = get_all_uids(self)
        bt.logging.info(f"✓ Found {len(miner_uids)} miners to evaluate: {sorted(miner_uids)}")

        if not miner_uids:
            bt.logging.warning("No miners found to evaluate. Skipping this round.")
        else:
            # Query miners and calculate rewards
            bt.logging.info("Starting miner evaluation process...")
            evaluation_start = time.time()
            rewards = await get_rewards(self, miner_uids, master_repositories, programming_languages)
            evaluation_time = time.time() - evaluation_start
            bt.logging.info(f"✓ Evaluation completed in {evaluation_time:.2f}s")

            # Log reward statistics
            non_zero_rewards = [r for r in rewards if r > 0]
            if non_zero_rewards:
                bt.logging.info(f"Reward statistics:")
                bt.logging.info(f"  - Total rewards distributed: {sum(rewards):.4f}")
                bt.logging.info(f"  - Miners with rewards: {len(non_zero_rewards)}/{len(rewards)}")
                bt.logging.info(f"  - Max reward: {max(rewards):.4f}")
                bt.logging.info(f"  - Min reward (non-zero): {min(non_zero_rewards):.4f}")
                bt.logging.info(f"  - Average reward: {sum(rewards)/len(rewards):.4f}")
            else:
                bt.logging.warning("No rewards were distributed this round")

            # Update scores
            bt.logging.info("Updating miner scores...")
            self.update_scores(rewards, miner_uids)
            bt.logging.info("✓ Scores updated successfully")

        round_time = time.time() - round_start_time
        bt.logging.info("="*70)
        bt.logging.info(f"***** Scoring round completed in {round_time:.2f}s *****")
        bt.logging.info(f"Next scoring round in {VALIDATOR_STEPS_INTERVAL} steps")
        bt.logging.info("="*70)

    await asyncio.sleep(VALIDATOR_WAIT)
