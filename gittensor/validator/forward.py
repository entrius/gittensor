# The MIT License (MIT)
# Copyright © 2025 Entrius

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Dict, Optional, Set, Tuple

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation, MinerEvaluationCache
from gittensor.utils.uids import get_all_uids
from gittensor.validator.oss_contributions.emission_allocate import build_round_reward_vector
from gittensor.validator.utils.config import (
    VALIDATOR_STEPS_INTERVAL,
    VALIDATOR_WAIT,
)

if TYPE_CHECKING:
    from gittensor.validator.utils.load_weights import RepositoryConfig
    from neurons.validator import Validator


async def forward(self: 'Validator') -> None:
    """Execute the validator's forward pass.

    Performs the core validation cycle every VALIDATOR_STEPS_INTERVAL steps:
    1. Score OSS contributions (mirror PR scoring)
    2. Score issue discovery
    3. Run issue bounties verification
    4. Store all evaluations to DB
    5. Allocate emissions (unified OSS pool + treasury + recycle slack)

    Emission policy:
    - Unified scoring pool: 90% of the round (``OSS_EMISSION_SHARE``), split
      across repos by ``emission_share`` and within each repo between PR and
      issue discovery via ``issue_discovery_share`` (with same-repo spill).
    - Issues treasury: 10% flat to UID 111.
    - Recycle (UID 0): registry slack ``(1 - sum(emission_share)) * OSS_EMISSION_SHARE``
      plus any repo slice where both PR and issue sides have no nonzero scorers
      in the window — no fixed baseline recycle floor.
    """

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        from gittensor.validator.utils.load_weights import (
            load_master_repo_weights,
            load_programming_language_weights,
            load_token_config,
        )

        miner_uids = get_all_uids(self)
        master_repositories = load_master_repo_weights()
        programming_languages = load_programming_language_weights()
        token_config = load_token_config()

        # 1. Score OSS contributions
        _, miner_evaluations, cached_uids, penalized_uids = await oss_contributions(
            self, miner_uids, master_repositories, programming_languages, token_config
        )

        # 2. Score issue discovery (mutates miner_evaluations)
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
        from gittensor.validator.issue_competitions.forward import issue_competitions

        await issue_competitions(self, miner_evaluations)

        # 4. Store all evaluations to DB (includes issue discovery fields)
        await self.bulk_store_evaluation(miner_evaluations, skip_uids=cached_uids)

        # 5. Allocate emissions (OSS pool + treasury + recycle slack)
        rewards = build_round_reward_vector(miner_evaluations, master_repositories, miner_uids)

        self.update_scores(rewards, miner_uids, blacklisted_uids=sorted(penalized_uids))

    await asyncio.sleep(VALIDATOR_WAIT)


async def oss_contributions(
    self: 'Validator',
    miner_uids: set[int],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict,
    token_config,
) -> Tuple[np.ndarray, Dict[int, MinerEvaluation], Set[int], Set[int]]:
    """Score OSS contributions and return placeholder rewards + miner evaluations + cached UIDs + penalized UIDs.

    Final per-UID emission fractions are produced by ``build_round_reward_vector``
    after issue discovery (see ``forward``).
    """
    from gittensor.validator.oss_contributions.reward import get_rewards

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
    """Score issue discovery and mutate ``miner_evaluations`` in place.

    Uses ``MirrorClient.get_miner_issues`` with authoritative ``solved_by_pr`` +
    inline ``solving_pr`` data, and a cross-miner cache of already-scored
    solving PRs so the base_score reflects real token scoring.
    """
    from gittensor.validator.issue_discovery.scan import run_issue_discovery

    await run_issue_discovery(
        miner_evaluations,
        master_repositories,
        programming_languages,
        token_config,
        evaluation_cache=evaluation_cache,
    )
