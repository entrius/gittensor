# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from typing import TYPE_CHECKING, Dict, Set, Tuple

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation
from gittensor.constants import (
    ISSUE_DISCOVERY_EMISSION_SHARE,
    ISSUES_TREASURY_EMISSION_SHARE,
    ISSUES_TREASURY_UID,
    OSS_EMISSION_SHARE,
    RECYCLE_EMISSION_SHARE,
    RECYCLE_UID,
)
from gittensor.utils.uids import get_all_uids
from gittensor.validator.issue_competitions.forward import issue_competitions
from gittensor.validator.issue_discovery.mirror_scan import run_mirror_issue_discovery
from gittensor.validator.issue_discovery.normalize import (
    normalize_issue_discovery_rewards,
)
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
    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        miner_uids = get_all_uids(self)
        master_repositories = load_master_repo_weights()
        programming_languages = load_programming_language_weights()
        token_config = load_token_config()

        # 1. Score OSS contributions
        oss_rewards, miner_evaluations, cached_uids, penalized_uids = await oss_contributions(
            self, miner_uids, master_repositories, programming_languages, token_config
        )

        # 2. Score issue discovery
        issue_rewards = await issue_discovery(
            miner_evaluations, master_repositories, programming_languages, token_config, miner_uids
        )

        # 3. Issue bounties verification
        await issue_competitions(self, miner_evaluations)

        # 4. Store all evaluations to DB (includes issue discovery fields)
        # cached_uids may have received fresh issue discovery data — persist it
        has_mirror_repos = any(cfg.mirror_enabled for cfg in master_repositories.values())
        if has_mirror_repos:
            cached_uids = set()
        await self.bulk_store_evaluation(miner_evaluations, skip_uids=cached_uids)

        # 5. Blend 4 emission pools into final rewards
        rewards = blend_emission_pools(oss_rewards, issue_rewards, miner_uids)

        self.update_scores(rewards, miner_uids, blacklisted_uids=sorted(penalized_uids))

    await asyncio.sleep(VALIDATOR_WAIT)


async def oss_contributions(
    self: 'Validator',
    miner_uids: set[int],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict,
    token_config,
) -> Tuple[np.ndarray, Dict[int, MinerEvaluation], Set[int], Set[int]]:
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
) -> np.ndarray:
    mirror_repos: Dict[str, RepositoryConfig] = {
        name: cfg for name, cfg in master_repositories.items() if cfg.mirror_enabled
    }

    if mirror_repos:
        await run_mirror_issue_discovery(miner_evaluations, mirror_repos, programming_languages, token_config)
    else:
        bt.logging.info('No mirror-enabled repos — issue discovery skipped for this round')

    issue_rewards_dict = normalize_issue_discovery_rewards(miner_evaluations)

    sorted_uids = sorted(miner_uids)
    return np.array([issue_rewards_dict.get(uid, 0.0) for uid in sorted_uids])


def blend_emission_pools(
    oss_rewards: np.ndarray,
    issue_rewards: np.ndarray,
    miner_uids: set[int],
) -> np.ndarray:
    sorted_uids = sorted(miner_uids)
    rewards = np.zeros(len(sorted_uids))
    recycle_extra = 0.0

    oss_total = float(oss_rewards.sum())
    if oss_total > 0:
        rewards += oss_rewards * OSS_EMISSION_SHARE
    else:
        recycle_extra += OSS_EMISSION_SHARE

    issue_total = float(issue_rewards.sum())
    if issue_total > 0:
        rewards += issue_rewards * ISSUE_DISCOVERY_EMISSION_SHARE
    else:
        recycle_extra += ISSUE_DISCOVERY_EMISSION_SHARE

    if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in miner_uids:
        treasury_idx = sorted_uids.index(ISSUES_TREASURY_UID)
        rewards[treasury_idx] += ISSUES_TREASURY_EMISSION_SHARE
        bt.logging.info(
            f'Treasury allocation: UID {ISSUES_TREASURY_UID} receives '
            f'{ISSUES_TREASURY_EMISSION_SHARE * 100:.0f}% of emissions'
        )

    if RECYCLE_UID in miner_uids:
        recycle_idx = sorted_uids.index(RECYCLE_UID)
        rewards[recycle_idx] += RECYCLE_EMISSION_SHARE + recycle_extra
        if recycle_extra > 0:
            bt.logging.info(f'Recycling {recycle_extra * 100:.0f}% unclaimed emissions from empty pools')

    return rewards
