# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from typing import TYPE_CHECKING, Dict, Set, Tuple

import bittensor as bt

from gittensor.classes import MinerEvaluation
from gittensor.utils.uids import get_all_uids
from gittensor.validator.emission_allocation import allocate_round_emissions
from gittensor.validator.issue_competitions.forward import issue_competitions
from gittensor.validator.issue_discovery.mirror_scan import run_mirror_issue_discovery
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
    1. Score OSS contributions (PR scoring — mirror path + legacy PAT path)
    2. Score issue discovery (mirror-only; non-mirror repos skip)
    3. Run issue bounties verification
    4. Store all evaluations to DB
    5. Allocate emissions (per-repo ``emission_share`` + within-repo PR/issue split) and update scores

    Monetary policy (see ``constants`` and ``emission_allocation``):
    - ``OSS_EMISSION_SHARE`` (90%) is the unified scoring pool: each repo receives
      its registry ``emission_share`` slice; recycle absorbs registry slack
      ``(1 - Σ emission_share) × OSS_EMISSION_SHARE`` plus any repo slice with no
      eligible nonzero-scored PR or issue activity in the round.
    - ``ISSUES_TREASURY_EMISSION_SHARE`` (10%) is flat to the treasury UID.
    """

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        miner_uids = get_all_uids(self)
        master_repositories = load_master_repo_weights()
        programming_languages = load_programming_language_weights()
        token_config = load_token_config()

        miner_evaluations, cached_uids, penalized_uids = await oss_contributions(
            self, miner_uids, master_repositories, programming_languages, token_config
        )

        await issue_discovery(miner_evaluations, master_repositories, programming_languages, token_config, miner_uids)

        await issue_competitions(self, miner_evaluations)

        await self.bulk_store_evaluation(miner_evaluations, skip_uids=cached_uids)

        rewards = allocate_round_emissions(miner_evaluations, master_repositories, miner_uids)

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

    Pure scoring — no DB storage or emission allocation. Those are handled by forward().
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
    miner_uids: set[int],
) -> None:
    """Score issue discovery (mirror-only). Mutates ``miner_evaluations`` in place.

    Uses ``MirrorClient.get_miner_issues`` with authoritative ``solved_by_pr`` +
    inline ``solving_pr`` data, and a cross-miner cache of already-scored solving
    PRs so the base_score reflects real token scoring. Non-mirror repos are skipped.

    Per-repo discovery weights for emission allocation are accumulated on each
    ``MinerEvaluation.issue_discovery_repo_scores`` (issue home repo).
    """
    mirror_repos: Dict[str, RepositoryConfig] = {
        name: cfg for name, cfg in master_repositories.items() if cfg.mirror_enabled
    }

    if mirror_repos:
        await run_mirror_issue_discovery(miner_evaluations, mirror_repos, programming_languages, token_config)
    else:
        bt.logging.info('No mirror-enabled repos — issue discovery skipped for this round')
