# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from typing import TYPE_CHECKING, Dict, Tuple

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation
from gittensor.constants import ISSUES_TREASURY_EMISSION_SHARE, ISSUES_TREASURY_UID, PREDICTIONS_EMISSIONS_SHARE
from gittensor.utils.uids import get_all_uids
from gittensor.validator.issue_competitions.forward import issue_competitions
from gittensor.validator.merge_predictions.settlement import merge_predictions
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
    1. Score OSS contributions (pure scoring, no side effects)
    2. Run issue bounties verification (needs tier data from scoring)
    3. Settle merge predictions (score + update EMAs)
    4. Build blended rewards array across all emission sources
    5. Update scores with blended rewards

    Emission blending:
    - OSS contributions: 70% (1.0 - treasury - predictions)
    - Issue bounties treasury: 15% flat to treasury UID
    - Merge predictions: 15% distributed by EMA scores
    """

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        miner_uids = get_all_uids(self)

        rewards, miner_evaluations = await oss_contributions(self, miner_uids)

        await issue_competitions(self, miner_evaluations)

        await merge_predictions(self, miner_evaluations)

        # Build blended rewards array across all emission sources
        oss_share = 1.0 - ISSUES_TREASURY_EMISSION_SHARE - PREDICTIONS_EMISSIONS_SHARE
        rewards *= oss_share

        if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in miner_uids:
            sorted_uids = sorted(miner_uids)
            treasury_idx = sorted_uids.index(ISSUES_TREASURY_UID)
            rewards[treasury_idx] = ISSUES_TREASURY_EMISSION_SHARE

            bt.logging.info(
                f'Treasury allocation: Smart Contract UID {ISSUES_TREASURY_UID} receives '
                f'{ISSUES_TREASURY_EMISSION_SHARE * 100:.0f}% of emissions'
            )

        prediction_rewards = build_prediction_ema_rewards(self, miner_uids, miner_evaluations)
        rewards += prediction_rewards

        bt.logging.info(
            f'Blended rewards: OSS {oss_share * 100:.0f}% + treasury {ISSUES_TREASURY_EMISSION_SHARE * 100:.0f}% '
            f'+ predictions {PREDICTIONS_EMISSIONS_SHARE * 100:.0f}% '
            f'(prediction sum={prediction_rewards.sum():.4f})'
        )

        self.update_scores(rewards, miner_uids)

    await asyncio.sleep(VALIDATOR_WAIT)


def build_prediction_ema_rewards(
    self: 'BaseValidatorNeuron',
    miner_uids: set[int],
    miner_evaluations: Dict[int, MinerEvaluation],
) -> np.ndarray:
    """Build rewards array from prediction EMA scores, scaled to PREDICTIONS_EMISSIONS_SHARE.

    Maps github_id-keyed EMAs back to UIDs via miner_evaluations.
    """
    sorted_uids = sorted(miner_uids)
    prediction_rewards = np.zeros(len(sorted_uids), dtype=np.float64)

    all_emas = self.mp_storage.get_all_emas()
    if not all_emas:
        return prediction_rewards

    # Build github_id -> uid mapping from current miner evaluations
    github_id_to_uid: Dict[str, int] = {}
    for uid, evaluation in miner_evaluations.items():
        if evaluation and evaluation.github_id and evaluation.github_id != '0':
            github_id_to_uid[evaluation.github_id] = uid

    for mp_record in all_emas:
        github_id = mp_record['github_id']
        ema_score = mp_record['ema_score']

        if ema_score <= 0:
            continue

        uid = github_id_to_uid.get(github_id)
        if uid is None or uid not in miner_uids:
            continue

        idx = sorted_uids.index(uid)
        prediction_rewards[idx] = ema_score

    # Normalize to sum=1.0, then scale to prediction share
    total = prediction_rewards.sum()
    if total > 0:
        prediction_rewards = (prediction_rewards / total) * PREDICTIONS_EMISSIONS_SHARE

    return prediction_rewards


async def oss_contributions(
    self: 'BaseValidatorNeuron', miner_uids: set[int]
) -> Tuple[np.ndarray, Dict[int, MinerEvaluation]]:
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
