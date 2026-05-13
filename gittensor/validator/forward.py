# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from typing import TYPE_CHECKING, Dict, Optional, Set, Tuple

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation, MinerEvaluationCache
from gittensor.utils.uids import get_all_uids
from gittensor.validator.emissions.allocate import allocate_emissions
from gittensor.validator.issue_competitions.forward import issue_competitions
from gittensor.validator.issue_discovery.scan import run_issue_discovery
from gittensor.validator.oss_contributions.reward import get_rewards
from gittensor.validator.utils.config import (
    VALIDATOR_STEPS_INTERVAL,
    VALIDATOR_WAIT,
)
from gittensor.validator.utils.load_weights import (
    RepositoryConfig,
    load_master_repo_weights,
    load_programming_language_weights,
    load_token_config,
)

if TYPE_CHECKING:
    from neurons.validator import Validator


async def forward(self: 'Validator') -> None:
    """Execute the validator's forward pass.

    Performs the core validation cycle every VALIDATOR_STEPS_INTERVAL steps:
    1. Score OSS contributions (mirror PR scoring)
    2. Score issue discovery
    3. Run issue bounties verification
    4. Store all evaluations to DB
    5. Allocate emissions and update scores

    Emission allocation:
    - OSS pool: 90%, split into bounded per-repository emission_share slices.
      Each repo slice is internally split between PR and issue-discovery scores.
    - Issue treasury: 10% flat to UID 111 when present.
    - Unclaimed repository slices and registry slack recycle to UID 0.
    """

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        miner_uids = get_all_uids(self)
        master_repositories = load_master_repo_weights()
        programming_languages = load_programming_language_weights()
        token_config = load_token_config()

        # 1. Score OSS contributions
        _oss_scores, miner_evaluations, cached_uids, penalized_uids = await oss_contributions(
            self, miner_uids, master_repositories, programming_languages, token_config
        )

        # 2. Score issue discovery
        await issue_discovery(
            miner_evaluations,
            master_repositories,
            programming_languages,
            token_config,
            miner_uids,
            evaluation_cache=self.evaluation_cache,
        )

        # cached UIDs now have fresh issue-discovery fields — persist them
        cached_uids.clear()

        # 3. Issue bounties verification
        await issue_competitions(self, miner_evaluations)

        # 4. Store all evaluations to DB (includes issue discovery fields)
        await self.bulk_store_evaluation(miner_evaluations, skip_uids=cached_uids)

        # 5. Allocate emissions by bounded per-repository slices.
        rewards = allocate_emissions(miner_evaluations, master_repositories, miner_uids)
        apply_collateral_deductions(rewards, miner_evaluations, miner_uids)

        self.update_scores(rewards, miner_uids, blacklisted_uids=sorted(penalized_uids))

    await asyncio.sleep(VALIDATOR_WAIT)


async def oss_contributions(
    self: 'Validator',
    miner_uids: set[int],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict,
    token_config,
) -> Tuple[np.ndarray, Dict[int, MinerEvaluation], Set[int], Set[int]]:
    """Score OSS contributions and return raw scores + miner evaluations + cached UIDs + penalized UIDs.

    Pure scoring — no DB storage or emission blending. Those are handled by forward().
    """
    tree_sitter_count = sum(1 for c in token_config.language_configs.values() if c.language is not None)

    bt.logging.info('***** Starting scoring round *****')
    bt.logging.info(f'Total Repositories loaded: {len(master_repositories)}')
    bt.logging.info(f'Total Languages loaded: {len(programming_languages)}')
    bt.logging.info(f'Token config: {tree_sitter_count} tree-sitter languages')
    bt.logging.info(f'Neurons to evaluate: {len(miner_uids)}')

    rewards, miner_evaluations, cached_uids, penalized_uids = await get_rewards(
        self, miner_uids, master_repositories, programming_languages, token_config
    )

    return rewards, miner_evaluations, cached_uids, penalized_uids


async def issue_discovery(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict,
    token_config,
    miner_uids: set[int],
    evaluation_cache: Optional[MinerEvaluationCache] = None,
) -> None:
    """Score issue discovery and populate miner evaluations.

    Uses ``MirrorClient.get_miner_issues`` with authoritative ``solved_by_pr`` +
    inline ``solving_pr`` data, and a cross-miner cache of already-scored
    solving PRs so the base_score reflects real token scoring.

    """
    await run_issue_discovery(
        miner_evaluations,
        master_repositories,
        programming_languages,
        token_config,
        evaluation_cache=evaluation_cache,
    )


def apply_collateral_deductions(
    rewards: np.ndarray,
    miner_evaluations: Dict[int, MinerEvaluation],
    miner_uids: set[int],
) -> None:
    """Deduct open-PR collateral from the scoring-pool allocation.

    Collateral is computed in raw PR-score units during finalization. The new
    allocator turns raw scores into shares of the OSS pool, so scale the
    deduction by the same pool before setting weights.
    """
    from gittensor.constants import OSS_EMISSION_SHARE

    for idx, uid in enumerate(sorted(miner_uids)):
        evaluation = miner_evaluations.get(uid)
        if evaluation is None or evaluation.total_collateral_score <= 0.0:
            continue
        rewards[idx] = max(0.0, rewards[idx] - evaluation.total_collateral_score * OSS_EMISSION_SHARE)
