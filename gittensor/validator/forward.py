# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from typing import TYPE_CHECKING, Dict, Tuple

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation
from gittensor.constants import (
    ISSUES_TREASURY_EMISSION_SHARE,
    ISSUES_TREASURY_UID,
)
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
    from neurons.validator import Validator


async def forward(self: 'Validator') -> None:
    """Execute the validator's forward pass.

    Performs the core validation cycle every VALIDATOR_STEPS_INTERVAL steps:
    1. Score OSS contributions (pure scoring, no side effects)
    2. Run issue bounties verification (needs eligibility data from scoring)
    3. Build blended rewards array with treasury allocation
    4. Update scores with blended rewards

    Emission blending:
    - OSS contributions: 85% (1.0 - treasury)
    - Issue bounties treasury: 15% flat to treasury UID
    """

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        miner_uids = get_all_uids(self)

        rewards, miner_evaluations = await oss_contributions(self, miner_uids)

        await issue_competitions(self, miner_evaluations)

        # Build blended rewards array with treasury allocation
        oss_share = 1.0 - ISSUES_TREASURY_EMISSION_SHARE
        rewards *= oss_share

        if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in miner_uids:
            sorted_uids = sorted(miner_uids)
            treasury_idx = sorted_uids.index(ISSUES_TREASURY_UID)
            rewards[treasury_idx] = ISSUES_TREASURY_EMISSION_SHARE

            bt.logging.info(
                f'Treasury allocation: Smart Contract UID {ISSUES_TREASURY_UID} receives '
                f'{ISSUES_TREASURY_EMISSION_SHARE * 100:.0f}% of emissions'
            )

        self.update_scores(rewards, miner_uids)

    await asyncio.sleep(VALIDATOR_WAIT)


async def oss_contributions(self: 'Validator', miner_uids: set[int]) -> Tuple[np.ndarray, Dict[int, MinerEvaluation]]:
    """Score OSS contributions and return raw rewards + miner evaluations.

    Pure scoring — no treasury allocation or weight updates. Those are
    handled by the caller (forward()).
    """
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

    return rewards, miner_evaluations
