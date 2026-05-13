# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from typing import TYPE_CHECKING, Dict, Optional, Set, Tuple

import bittensor as bt
import numpy as np

from gittensor.classes import MinerEvaluation, MinerEvaluationCache
from gittensor.constants import (
    ISSUES_TREASURY_EMISSION_SHARE,
    ISSUES_TREASURY_UID,
    RECYCLE_UID,
)
from gittensor.utils.uids import get_all_uids
from gittensor.validator.issue_competitions.forward import issue_competitions
from gittensor.validator.issue_discovery.normalize import (
    normalize_issue_discovery_rewards,
)
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
    from typing import Dict, Optional

    from gittensor.classes import MinerEvaluation
    from gittensor.validator.utils.load_weights import RepositoryConfig
    from neurons.validator import Validator


async def forward(self: 'Validator') -> None:
    """Execute the validator's forward pass.

    Performs the core validation cycle every VALIDATOR_STEPS_INTERVAL steps:
    1. Score OSS contributions (mirror PR scoring)
    2. Score issue discovery
    3. Run issue bounties verification
    4. Store all evaluations to DB
    5. Blend emission pools and update scores

    Emission blending:
    - Combined scoring (OSS + issue discovery): 90%
    - Issue treasury:                           10% (flat to UID 111)
    - Recycle:                                  remainder (no fixed baseline)
    """

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

        # 5. Blend 4 emission pools into final rewards
        rewards = blend_emission_pools(
            oss_rewards,
            issue_rewards,
            miner_uids,
            miner_evaluations=miner_evaluations,
            master_repositories=master_repositories,
        )

        self.update_scores(rewards, miner_uids, blacklisted_uids=sorted(penalized_uids))

    await asyncio.sleep(VALIDATOR_WAIT)


async def oss_contributions(
    self: 'Validator',
    miner_uids: set[int],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict,
    token_config,
) -> Tuple[np.ndarray, Dict[int, MinerEvaluation], Set[int], Set[int]]:
    """Score OSS contributions and return normalized rewards + miner evaluations + cached UIDs + penalized UIDs.

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
) -> np.ndarray:
    """Score issue discovery and return normalized rewards array.

    Uses ``MirrorClient.get_miner_issues`` with authoritative ``solved_by_pr`` +
    inline ``solving_pr`` data, and a cross-miner cache of already-scored
    solving PRs so the base_score reflects real token scoring.

    Returns numpy array of normalized issue discovery rewards (sorted by UID).
    """
    await run_issue_discovery(
        miner_evaluations,
        master_repositories,
        programming_languages,
        token_config,
        evaluation_cache=evaluation_cache,
    )

    issue_rewards_dict = normalize_issue_discovery_rewards(miner_evaluations)

    sorted_uids = sorted(miner_uids)
    return np.array([issue_rewards_dict.get(uid, 0.0) for uid in sorted_uids])


def blend_emission_pools(
    oss_rewards: np.ndarray,
    issue_rewards: np.ndarray,
    miner_uids: set[int],
    oss_pool_share: float = 0.90,
    miner_evaluations: Optional[Dict[int, 'MinerEvaluation']] = None,
    master_repositories: Optional[Dict[str, 'RepositoryConfig']] = None,
) -> np.ndarray:
    """Blend emission pools into a single rewards array.

    When *miner_evaluations* and *master_repositories* are provided, the
    combined scoring pool is distributed per-repo by ``emission_share``:
    each repo receives ``emission_share × pool`` and divides it
    proportionally among its eligible miners by their per-repo earned score.

    Without repo data, falls back to a flat per-UID distribution.

    - Combined scoring pool (OSS + issue discovery): {oss_pool_share:.0%}
    - Issue treasury:                        10% (flat to UID 111)
    - Recycle:                               remainder (no fixed baseline)
    """
    sorted_uids = sorted(miner_uids)
    rewards = np.zeros(len(sorted_uids))
    recycle_extra = 0.0

    # Pool 1: Combined scoring (OSS + issue discovery)
    oss_total = float(oss_rewards.sum())
    issue_total = float(issue_rewards.sum())
    combined_total = oss_total + issue_total

    if combined_total > 0 and miner_evaluations and master_repositories:
        total_emission = sum(c.emission_share for c in master_repositories.values())
        if total_emission > 0:
            uid_to_idx = {uid: i for i, uid in enumerate(sorted_uids)}

            # Build per-repo, per-UID score map from miner evaluations
            repo_scores: Dict[str, Dict[int, float]] = {}
            for uid, eval_ in miner_evaluations.items():
                if uid not in uid_to_idx:
                    continue
                for scored_pr in getattr(eval_, 'merged_prs', []):
                    repo = getattr(getattr(scored_pr, 'pr', None), 'repo_full_name', None) or ''
                    if repo in master_repositories:
                        repo_scores.setdefault(repo, {}).setdefault(uid, 0.0)
                        repo_scores[repo][uid] += getattr(scored_pr, 'earned_score', 0.0) or 0.0

            # Distribute per-repo
            if repo_scores:
                for repo_name, cfg in master_repositories.items():
                    uid_scores = repo_scores.get(repo_name, {})
                    if not uid_scores:
                        continue
                    total_score = sum(uid_scores.values())
                    if total_score <= 0:
                        continue
                    repo_allocation = (cfg.emission_share / total_emission) * combined_total * oss_pool_share
                    for uid, score in uid_scores.items():
                        rewards[uid_to_idx[uid]] += repo_allocation * (score / total_score)
                return rewards

        # Fallback to flat distribution if repo-level data is incomplete
        combined_rewards = oss_rewards + issue_rewards
        rewards += combined_rewards * oss_pool_share
    elif combined_total > 0:
        combined_rewards = oss_rewards + issue_rewards
        rewards += combined_rewards * oss_pool_share
    else:
        recycle_extra += oss_pool_share

    # Pool 2: Issue treasury (10% flat to UID 111)
    if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in miner_uids:
        treasury_idx = sorted_uids.index(ISSUES_TREASURY_UID)
        rewards[treasury_idx] += ISSUES_TREASURY_EMISSION_SHARE
        bt.logging.info(
            f'Treasury allocation: UID {ISSUES_TREASURY_UID} receives '
            f'{ISSUES_TREASURY_EMISSION_SHARE * 100:.0f}% of emissions'
        )

    # Pool 3: Recycle (unclaimed from empty pools)
    if RECYCLE_UID in miner_uids:
        recycle_idx = sorted_uids.index(RECYCLE_UID)
        rewards[recycle_idx] += recycle_extra
        if recycle_extra > 0:
            bt.logging.info(f'Recycling {recycle_extra * 100:.0f}% unclaimed emissions from empty pools')

    return rewards
