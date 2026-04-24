"""Fetch and bucket a miner's PRs via the das-github-mirror service.

Counterpart to ``gittensor.utils.github_api_tools.load_miners_prs`` but for
the mirror path. The mirror returns one bundle per PR (with all scoring
inputs inlined), so loading is a single HTTP call regardless of how many
mirror-enabled repos the miner has touched.

Filtering applied at load time:
- Repo not in mirror_repos: dropped (mirror returns all tracked repos)
- Repo inactive at PR creation time: dropped
- PR author is a maintainer (OWNER/MEMBER/COLLABORATOR): dropped (matches legacy)
- CLOSED PRs created before the lookback window: dropped (matches legacy)

All other eligibility (base_ref check, edited_after_merge anti-gaming, etc.)
is enforced during scoring, not loading.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import bittensor as bt

from gittensor.constants import MAINTAINER_ASSOCIATIONS, PR_LOOKBACK_DAYS
from gittensor.utils.mirror.client import MirrorClient, MirrorRequestError
from gittensor.utils.mirror.models import MirrorPullRequest
from gittensor.validator.oss_contributions.mirror.evaluation import MirrorMinerEvaluation
from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredMirrorPR
from gittensor.validator.oss_contributions.mirror.scoring import _should_skip_merged_mirror_pr
from gittensor.validator.utils.datetime_utils import parse_github_iso_to_utc
from gittensor.validator.utils.load_weights import RepositoryConfig


def load_mirror_miner_prs(
    mirror_eval: MirrorMinerEvaluation,
    mirror_repos: Dict[str, RepositoryConfig],
    client: Optional[MirrorClient] = None,
) -> None:
    """Populate mirror_eval with PRs fetched from the mirror service.

    Args:
        mirror_eval: container to populate; must already have github_id set
        mirror_repos: repo configs to filter against (only mirror_enabled entries)
        client: optional MirrorClient for dependency injection in tests
    """

    bt.logging.info('*****Fetching PRs from mirror*****')
    mirror_eval.fetch_failed = False

    if not mirror_eval.github_id:
        bt.logging.warning(f'UID {mirror_eval.uid} has no github_id, skipping mirror fetch')
        return

    if not mirror_repos:
        bt.logging.info(f'UID {mirror_eval.uid} has no mirror-enabled repos, skipping mirror fetch')
        return

    client = client or MirrorClient()
    lookback_date = datetime.now(timezone.utc) - timedelta(days=PR_LOOKBACK_DAYS)

    try:
        response = client.get_miner_pulls(mirror_eval.github_id, since=lookback_date)
    except MirrorRequestError as e:
        bt.logging.error(f'Mirror fetch failed for UID {mirror_eval.uid}: {e}')
        mirror_eval.fetch_failed = True
        return

    for pr in response.pull_requests:
        try:
            _maybe_add_pr(mirror_eval, pr, mirror_repos, lookback_date)
        except Exception as e:
            bt.logging.warning(f'Error processing mirror PR #{pr.pr_number} ({pr.repo_full_name}): {e}')

    bt.logging.info(
        f'Mirror fetched {len(mirror_eval.merged_prs)} merged, '
        f'{len(mirror_eval.open_prs)} open, {len(mirror_eval.closed_prs)} closed'
    )


def _maybe_add_pr(
    mirror_eval: MirrorMinerEvaluation,
    pr: MirrorPullRequest,
    mirror_repos: Dict[str, RepositoryConfig],
    lookback_date: datetime,
) -> None:
    """Apply load-time filters and bucket pr by state if it passes."""

    repo_config = mirror_repos.get(pr.repo_full_name) or mirror_repos.get(pr.repo_full_name.lower())
    if repo_config is None:
        bt.logging.info(
            f'Skipping mirror PR #{pr.pr_number} in {pr.repo_full_name} - not in mirror_repos'
        )
        return

    # Skip if the repo was deactivated before this PR was created
    if repo_config.inactive_at is not None:
        inactive_dt = parse_github_iso_to_utc(repo_config.inactive_at)
        if pr.created_at >= inactive_dt:
            bt.logging.info(
                f'Skipping mirror PR #{pr.pr_number} in {pr.repo_full_name} - '
                f'created after repo became inactive'
            )
            return

    # Skip maintainer-authored PRs (consistent with legacy load behavior).
    # DEV_MODE bypasses this so local testing can score maintainer PRs.
    if not os.environ.get('DEV_MODE') and pr.author_association in MAINTAINER_ASSOCIATIONS:
        bt.logging.info(
            f'Skipping mirror PR #{pr.pr_number} in {pr.repo_full_name} - '
            f'author is {pr.author_association}'
        )
        return

    if pr.state == 'OPEN':
        mirror_eval.open_prs.append(ScoredMirrorPR(pr=pr))
    elif pr.state == 'CLOSED':
        # Skip stale CLOSED PRs created before the lookback window (legacy parity:
        # closing an old PR shouldn't trigger a fresh credibility penalty).
        if pr.created_at < lookback_date:
            return
        mirror_eval.closed_prs.append(ScoredMirrorPR(pr=pr))
    elif pr.state == 'MERGED':
        # Apply the merge-eligibility gate at LOAD time (matches legacy parity —
        # should_skip_merged_pr runs inside load_miners_prs before adding). If we
        # deferred to scoring, rejected PRs would remain in mirror_merged_prs and
        # inflate the merged_count used in check_eligibility, distorting credibility.
        candidate = ScoredMirrorPR(pr=pr)
        should_skip, reason = _should_skip_merged_mirror_pr(candidate, repo_config)
        if should_skip:
            bt.logging.debug(reason or '')
            return
        mirror_eval.merged_prs.append(candidate)
    else:
        bt.logging.warning(f'Unknown PR state {pr.state!r} for PR #{pr.pr_number}')
