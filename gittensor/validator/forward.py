# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from typing import TYPE_CHECKING, Dict, Optional, Set, Tuple

import bittensor as bt

from gittensor.classes import MinerEvaluation, MinerEvaluationCache
from gittensor.utils.mirror.client import MirrorClient, MirrorRequestError
from gittensor.utils.uids import get_all_uids
from gittensor.validator.emission_allocation import blend_emission_pools
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
    5. Blend emission pools and update scores

    Emission blending:
    - Combined scoring pool: 90%, allocated by repository emission_share
    - Maintainer cut:        per-repo carve-out routed to maintainer miner neurons
    - Issue treasury:       10%, flat to UID 111
    - Recycle:              registry slack and inactive repo slices to UID 0
    """

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        miner_uids = get_all_uids(self)
        master_repositories = load_master_repo_weights()
        programming_languages = load_programming_language_weights()
        token_config = load_token_config()

        # 1. Score OSS contributions
        miner_evaluations, cached_uids, penalized_uids = await oss_contributions(
            self, miner_uids, master_repositories, programming_languages, token_config
        )

        # 2. Score issue discovery
        await issue_discovery(
            miner_evaluations,
            master_repositories,
            programming_languages,
            token_config,
            evaluation_cache=self.evaluation_cache,
        )

        # cached UIDs now have fresh issue-discovery fields — persist them
        cached_uids.clear()

        # 3. Issue bounties verification
        await issue_competitions(self, miner_evaluations)

        # 4. Store all evaluations to DB (includes issue discovery fields)
        await self.bulk_store_evaluation(miner_evaluations, master_repositories, skip_uids=cached_uids)

        # 5. Allocate repo-bounded emission shares into final rewards
        maintainer_uids_by_repo = _build_maintainer_uids_by_repo(miner_evaluations, master_repositories, miner_uids)
        rewards = blend_emission_pools(miner_evaluations, master_repositories, miner_uids, maintainer_uids_by_repo)

        self.update_scores(rewards, miner_uids, blacklisted_uids=sorted(penalized_uids))

    await asyncio.sleep(VALIDATOR_WAIT)


async def oss_contributions(
    self: 'Validator',
    miner_uids: set[int],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict,
    token_config,
) -> Tuple[Dict[int, MinerEvaluation], Set[int], Set[int]]:
    """Score OSS contributions and return miner evaluations + cached UIDs + penalized UIDs.

    Pure scoring — no DB storage or emission blending. Those are handled by forward().
    """
    tree_sitter_count = sum(1 for c in token_config.language_configs.values() if c.language is not None)

    bt.logging.info('***** Starting scoring round *****')
    bt.logging.info(f'Total Repositories loaded: {len(master_repositories)}')
    bt.logging.info(f'Total Languages loaded: {len(programming_languages)}')
    bt.logging.info(f'Token config: {tree_sitter_count} tree-sitter languages')
    bt.logging.info(f'Neurons to evaluate: {len(miner_uids)}')

    miner_evaluations, cached_uids, penalized_uids = await get_rewards(
        self, miner_uids, master_repositories, programming_languages, token_config
    )

    return miner_evaluations, cached_uids, penalized_uids


async def issue_discovery(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict,
    token_config,
    evaluation_cache: Optional[MinerEvaluationCache] = None,
) -> None:
    """Score issue discovery fields on miner evaluations.

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


def _build_maintainer_uids_by_repo(
    miner_evaluations: Dict[int, MinerEvaluation],
    master_repositories: Dict[str, RepositoryConfig],
    miner_uids: set[int],
) -> Dict[str, list[int]]:
    """Map repo name -> sorted registered maintainer-miner UIDs for the
    ``maintainer_cut`` carve-out.

    The mirror is queried only for repos with ``maintainer_cut > 0``. A repo
    whose lookup fails, or that has no registered maintainer miners, is omitted
    so ``blend_emission_pools`` skips the carve-out and scores the slice
    normally. Must run after ``miner_evaluations`` is fully populated so every
    UID's ``github_id`` is known.
    """
    repos_needing = {name: cfg for name, cfg in master_repositories.items() if cfg.maintainer_cut > 0.0}
    if not repos_needing:
        return {}

    github_id_to_uid: Dict[str, int] = {}
    for uid, evaluation in miner_evaluations.items():
        if uid not in miner_uids:
            continue
        github_id = evaluation.github_id
        if github_id and github_id != '0':
            github_id_to_uid[str(github_id)] = uid

    result: Dict[str, list[int]] = {}
    with MirrorClient() as client:
        for repo_name in repos_needing:
            try:
                response = client.get_repo_maintainers(repo_name)
            except MirrorRequestError as e:
                bt.logging.warning(
                    f'maintainer_cut: mirror maintainer lookup failed for {repo_name} ({e}); '
                    f'skipping carve-out, slice scores normally'
                )
                continue
            uids = sorted(
                {github_id_to_uid[m.github_id] for m in response.maintainers if m.github_id in github_id_to_uid}
            )
            if uids:
                result[repo_name] = uids
            else:
                bt.logging.info(
                    f'maintainer_cut: no registered maintainer miners for {repo_name}; '
                    f'carve-out skipped, slice scores normally'
                )
    return result
