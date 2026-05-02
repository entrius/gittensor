# The MIT License (MIT)
# Copyright © 2025 Entrius

from typing import Dict

import bittensor as bt

from gittensor.classes import MinerEvaluation


def normalize_issue_discovery_rewards(
    miner_evaluations: Dict[int, MinerEvaluation],
    reward_coverage: float = 1.0,
) -> Dict[int, float]:
    """Normalize issue discovery scores while preserving unavailable fetch share.

    ``reward_coverage`` is the fraction of attempted mirror issue fetches that
    succeeded this round. When it is below 1.0, only that fraction of the issue
    pool is allocated to scored miners; the remainder is recycled by the caller.
    """

    if not miner_evaluations:
        return {}

    rewards: Dict[int, float] = {}
    nonzero_count = 0

    for uid, evaluation in miner_evaluations.items():
        rewards[uid] = evaluation.issue_discovery_score
        if rewards[uid] > 0:
            nonzero_count += 1

    total = sum(rewards.values())
    if total <= 0:
        bt.logging.info('Issue discovery: all scores are zero, returning empty rewards')
        return rewards

    coverage = max(0.0, min(1.0, reward_coverage))
    normalized = {uid: (score / total) * coverage for uid, score in rewards.items()}

    bt.logging.info(f'Issue discovery: normalized {nonzero_count} miners with scores > 0')
    if coverage < 1.0:
        bt.logging.warning(f'Issue discovery: withholding {(1.0 - coverage) * 100:.1f}% due to mirror fetch errors')

    return normalized
