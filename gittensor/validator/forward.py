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
from gittensor.validator.issue_discovery.normalize import (
    normalize_issue_discovery_rewards,
)
from gittensor.validator.issue_discovery.repo_scan import scan_closed_issues
from gittensor.validator.issue_discovery.scoring import score_discovered_issues
from gittensor.validator.oss_contributions.reward import get_rewards
from gittensor.validator.utils.config import (
    GITTENSOR_VALIDATOR_PAT,
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
    1. Score OSS contributions (PR scoring)
    2. Run issue bounties verification
    3. Score issue discovery (repo scan + scoring)
    4. Store all evaluations to DB
    5. Blend emission pools and update scores

    Emission blending (hardcoded per-competition):
    - OSS contributions: 30%
    - Issue discovery:   30%
    - Issue treasury:    15% (flat to UID 111)
    - Recycle:           25% (flat to UID 0)
    """

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        miner_uids = get_all_uids(self)
        master_repositories = load_master_repo_weights()

        # 1. Score OSS contributions
        oss_rewards, miner_evaluations, cached_uids = await oss_contributions(self, miner_uids, master_repositories)

        # 2. Issue bounties verification
        await issue_competitions(self, miner_evaluations)

        # 3. Score issue discovery
        issue_rewards = await issue_discovery(miner_evaluations, master_repositories, miner_uids)

        # 4. Store all evaluations to DB (includes issue discovery fields)
        await self.bulk_store_evaluation(miner_evaluations, skip_uids=cached_uids)

        # 5. Blend 4 emission pools into final rewards
        rewards = blend_emission_pools(oss_rewards, issue_rewards, miner_uids)

        self.update_scores(rewards, miner_uids)

    await asyncio.sleep(VALIDATOR_WAIT)


async def oss_contributions(
    self: 'Validator',
    miner_uids: set[int],
    master_repositories: Dict[str, RepositoryConfig],
) -> Tuple[np.ndarray, Dict[int, MinerEvaluation], Set[int]]:
    """Score OSS contributions and return normalized rewards + miner evaluations + cached UIDs.

    Pure scoring — no DB storage or emission blending. Those are handled by forward().
    """
    programming_languages = load_programming_language_weights()
    token_config = load_token_config()

    tree_sitter_count = sum(1 for c in token_config.language_configs.values() if c.language is not None)

    bt.logging.info('***** Starting scoring round *****')
    bt.logging.info(f'Total Repositories loaded: {len(master_repositories)}')
    bt.logging.info(f'Total Languages loaded: {len(programming_languages)}')
    bt.logging.info(f'Token config: {tree_sitter_count} tree-sitter languages')
    bt.logging.info(f'Neurons to evaluate: {len(miner_uids)}')

    rewards, miner_evaluations, cached_uids = await get_rewards(
        self, miner_uids, master_repositories, programming_languages, token_config
    )

    return rewards, miner_evaluations, cached_uids


async def issue_discovery(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    miner_uids: set[int],
) -> np.ndarray:
    """Score issue discovery and return normalized rewards array.

    1. Scan tracked repos for miner-authored closed issues (validator PAT)
    2. Score issue discovery using PR-linked issues + scan results
    3. Normalize into independent pool

    Returns numpy array of normalized issue discovery rewards (sorted by UID).
    """
    # Scan tracked repos for closed issues not linked to miner PRs
    scan_issues: Dict[str, list] = {}
    if GITTENSOR_VALIDATOR_PAT:
        scan_issues = await scan_closed_issues(miner_evaluations, master_repositories, GITTENSOR_VALIDATOR_PAT)

    # Score issue discovery
    score_discovered_issues(miner_evaluations, master_repositories, scan_issues)

    # Normalize into independent pool
    issue_rewards_dict = normalize_issue_discovery_rewards(miner_evaluations)

    sorted_uids = sorted(miner_uids)
    return np.array([issue_rewards_dict.get(uid, 0.0) for uid in sorted_uids])


def blend_emission_pools(
    oss_rewards: np.ndarray,
    issue_rewards: np.ndarray,
    miner_uids: set[int],
) -> np.ndarray:
    """Blend 4 emission pools into a single rewards array.

    - OSS contributions: 30%
    - Issue discovery:   30%
    - Issue treasury:    15% (flat to UID 111)
    - Recycle:           25% (flat to UID 0)
    """
    sorted_uids = sorted(miner_uids)
    rewards = np.zeros(len(sorted_uids))

    # Pool 1: OSS contributions (30%)
    rewards += oss_rewards * OSS_EMISSION_SHARE

    # Pool 2: Issue discovery (30%)
    rewards += issue_rewards * ISSUE_DISCOVERY_EMISSION_SHARE

    # Pool 3: Issue treasury (15% flat to UID 111)
    if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in miner_uids:
        treasury_idx = sorted_uids.index(ISSUES_TREASURY_UID)
        rewards[treasury_idx] += ISSUES_TREASURY_EMISSION_SHARE
        bt.logging.info(
            f'Treasury allocation: UID {ISSUES_TREASURY_UID} receives '
            f'{ISSUES_TREASURY_EMISSION_SHARE * 100:.0f}% of emissions'
        )

    # Pool 4: Recycle (25% flat to UID 0)
    if RECYCLE_UID in miner_uids:
        recycle_idx = sorted_uids.index(RECYCLE_UID)
        rewards[recycle_idx] += RECYCLE_EMISSION_SHARE

    return rewards
