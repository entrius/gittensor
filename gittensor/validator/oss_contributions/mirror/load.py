"""Fetch and bucket a miner's PRs via the das-github-mirror service.

The mirror returns one bundle per PR (with all scoring inputs inlined), so
loading is a single HTTP call regardless of how many repos the miner has
touched. The call sends each repo's ``pr_lookback_days`` window, so the mirror
applies the per-repo time cutoffs server-side and returns only in-window PRs.

Filtering applied at load time:
- Repo not in master_repositories: dropped (defensive — the per-repo request
  already scopes the response to the registered repos).
- PR author is a maintainer (OWNER/MEMBER/COLLABORATOR): silently dropped.
- MERGED PRs that fail ``_should_skip_merged_mirror_pr`` (base_ref, head_ref,
  self-merge w/o approval, etc.): dropped. Applied at LOAD time so the
  merged_count used by ``check_eligibility`` isn't inflated by ineligible PRs.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import bittensor as bt

from gittensor.classes import MinerEvaluation
from gittensor.constants import MAINTAINER_ASSOCIATIONS
from gittensor.utils.mirror.client import MirrorClient, MirrorRequestError
from gittensor.utils.mirror.models import MirrorPullRequest
from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR
from gittensor.validator.oss_contributions.mirror.scoring import _should_skip_merged_mirror_pr
from gittensor.validator.utils.config import dev_mode_enabled
from gittensor.validator.utils.load_weights import RepositoryConfig, resolve_scoring


def load_miner_prs(
    eval_: MinerEvaluation,
    master_repositories: Dict[str, RepositoryConfig],
    client: Optional[MirrorClient] = None,
) -> None:
    """Populate eval_ with PRs fetched from the mirror service.

    Args:
        eval_: MinerEvaluation to populate; must already have github_id set.
        master_repositories: repo configs to filter against.
        client: optional MirrorClient for dependency injection in tests.

    On fetch failure both ``mirror_pr_fetch_failed`` and ``github_pr_fetch_failed``
    are set on the eval; the latter drives the cache-fallback path.
    """

    bt.logging.info('***** Fetching PRs *****')

    if not eval_.github_id:
        bt.logging.warning(f'UID {eval_.uid} has no github_id, skipping PR fetch')
        return

    if not master_repositories:
        bt.logging.info(f'UID {eval_.uid} has no scoring repos, skipping PR fetch')
        return

    client = client or MirrorClient()
    now = datetime.now(timezone.utc)
    # Each repo is windowed by its own pr_lookback_days; the mirror applies the
    # per-repo cutoffs server-side and returns only in-window PRs.
    since_by_repo = {
        name: now - timedelta(days=resolve_scoring(rc.scoring).pr_lookback_days)
        for name, rc in master_repositories.items()
    }

    try:
        response = client.get_miner_pulls(eval_.github_id, since_by_repo=since_by_repo)
    except MirrorRequestError as e:
        bt.logging.error(f'PR fetch failed for UID {eval_.uid}: {e}')
        eval_.mirror_pr_fetch_failed = True
        eval_.github_pr_fetch_failed = True
        return

    for pr in response.pull_requests:
        try:
            _maybe_add_pr(eval_, pr, master_repositories)
        except Exception as e:
            bt.logging.warning(f'Error processing PR #{pr.pr_number} ({pr.repo_full_name}): {e}')

    bt.logging.info(
        f'Fetched {len(eval_.merged_prs)} merged, {len(eval_.open_prs)} open, {len(eval_.closed_prs)} closed'
    )


def _maybe_add_pr(
    eval_: MinerEvaluation,
    pr: MirrorPullRequest,
    master_repositories: Dict[str, RepositoryConfig],
) -> None:
    """Apply load-time filters and bucket pr by state if it passes.

    Time-windowing (each repo's ``pr_lookback_days``) is applied by the mirror,
    so every PR here is already inside its repo's window.
    """

    repo_config = master_repositories.get(pr.repo_full_name)
    if repo_config is None:
        # Defensive: the per-repo request already scopes the response, but a
        # stray repo would otherwise have no config to score against.
        bt.logging.debug(f'Skipping PR #{pr.pr_number} in {pr.repo_full_name} - not in master_repositories')
        return

    # Silent maintainer skip — logging every maintainer-merged PR would dominate
    # the skip-reason log.
    if not dev_mode_enabled() and pr.author_association in MAINTAINER_ASSOCIATIONS:
        return

    if pr.state == 'OPEN':
        eval_.open_prs.append(ScoredPR(pr=pr))
    elif pr.state == 'CLOSED':
        eval_.closed_prs.append(ScoredPR(pr=pr))
    elif pr.state == 'MERGED':
        # Apply the merge-eligibility gate at LOAD time so the merged_count used
        # by check_eligibility isn't inflated by PRs that would be rejected.
        # Deferring to scoring would let rejected PRs sit in merged_prs
        # and distort credibility.
        candidate = ScoredPR(pr=pr)
        should_skip, reason = _should_skip_merged_mirror_pr(candidate, repo_config)
        if should_skip:
            bt.logging.debug(reason or '')
            return
        eval_.merged_prs.append(candidate)
    else:
        bt.logging.warning(f'Unknown PR state {pr.state!r} for PR #{pr.pr_number}')
