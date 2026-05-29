"""Per-PR scoring for the mirror path.

Scope:
- Compute base_score for each PR via the existing token-scoring infra.
- Compute per-PR multipliers: time_decay, review_quality, label, issue.
- The merge-eligibility gate (``_should_skip_merged_mirror_pr``) is exported and
  applied at LOAD time by ``mirror.load._maybe_add_pr`` — rejected PRs never
  enter ``merged_prs``, so the merged_count used by ``check_eligibility``
  is not inflated by PRs we would have rejected.

Cross-path concerns handled by ``finalize_miner_scores`` in
``gittensor.validator.oss_contributions.scoring`` (walks ``merged_prs``):
spam_multiplier, credibility_multiplier, final earned_score
composition, and base/earned/nodes aggregation.

Anti-gaming notes:
- ``edited_after_merge`` is NOT a PR-level gate — it gates only the issue
  bonus multiplier in ``_is_valid_linked_issue``.
- ``resolve_trusted_label_multiplier`` applies the shared maintainer/trusted
  label gate, including GitHub-App-applied labels (NULL ``actor_association``)
  for repos with an authoritative labeler.
"""

import asyncio
import math
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import bittensor as bt

from gittensor.classes import FileChange, MinerEvaluation, PrScoringResult, ScoringCategory
from gittensor.constants import (
    CONTRIBUTION_SCORE_FOR_FULL_BONUS,
    MAINTAINER_ASSOCIATIONS,
    MAX_CONTRIBUTION_BONUS,
    MAX_ISSUE_CLOSE_WINDOW_DAYS,
    MERGED_PR_BASE_SCORE,
    SECONDS_PER_DAY,
    SRC_TOK_SATURATION_SCALE,
)
from gittensor.utils.github_api_tools import FileContentPair, branch_matches_pattern
from gittensor.utils.mirror.client import MirrorClient, MirrorRequestError
from gittensor.utils.mirror.models import MirrorLinkedIssue, MirrorPullRequest
from gittensor.validator.oss_contributions.label_resolution import resolve_trusted_label_multiplier
from gittensor.validator.oss_contributions.mirror.adapters import mirror_files_to_legacy
from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR
from gittensor.validator.oss_contributions.scoring import (
    calculate_review_quality_multiplier,
)
from gittensor.validator.utils.datetime_utils import calculate_time_decay
from gittensor.validator.utils.load_weights import (
    LanguageConfig,
    RepositoryConfig,
    ResolvedScoring,
    TokenConfig,
    resolve_scoring,
)
from gittensor.validator.utils.tree_sitter_scoring import calculate_token_score_from_file_changes

# ============================================================================
# Entry point
# ============================================================================


async def score_miner_prs(
    eval_: MinerEvaluation,
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
    client: Optional[MirrorClient] = None,
) -> None:
    """Score all PRs on a MinerEvaluation.

    Mutates the eval: per-PR sets base_score + multipliers; tracks unique
    repos. Token totals are aggregated later in ``finalize_miner_scores``.
    """
    bt.logging.info('')
    bt.logging.info('-' * 50)
    bt.logging.info(
        f'Scoring UID {eval_.uid}: '
        f'{len(eval_.merged_prs)} merged | '
        f'{len(eval_.open_prs)} open | '
        f'{len(eval_.closed_prs)} closed'
    )
    bt.logging.info('-' * 50)

    client = client or MirrorClient()

    pr_groups = [
        ('MERGED', eval_.merged_prs),
        ('OPEN', eval_.open_prs),
        ('CLOSED', eval_.closed_prs),
    ]

    for label, scored_prs in pr_groups:
        for i, scored in enumerate(scored_prs, start=1):
            bt.logging.info(
                f'\n[{i}/{len(scored_prs)}] {label} PR #{scored.pr.pr_number} in {scored.pr.repo_full_name}'
            )
            try:
                await score_pr(scored, eval_, master_repositories, programming_languages, token_config, client)
            except Exception as e:
                bt.logging.warning(
                    f'UID {eval_.uid}: scoring failed for PR #{scored.pr.pr_number} in {scored.pr.repo_full_name}: {e}'
                )


# ============================================================================
# Per-PR scoring
# ============================================================================


async def score_pr(
    scored: ScoredPR,
    eval_: MinerEvaluation,
    master_repositories: Dict[str, RepositoryConfig],
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
    client: MirrorClient,
) -> None:
    """Score a single PR. Populates ScoredPR scoring fields in place."""
    pr = scored.pr
    repo_config = master_repositories.get(pr.repo_full_name)
    if not repo_config:
        bt.logging.warning(f'{pr.repo_full_name} not in master_repositories. Skipping...')
        return

    # Eligibility gate for MERGED PRs already ran at LOAD time (see
    # load_miner_prs → _should_skip_merged_mirror_pr). By this point
    # eval_.merged_prs contains only eligibility-passed PRs.

    has_fixed_base = repo_config.fixed_base_score is not None
    scoring_cfg = resolve_scoring(repo_config.scoring)

    # Mirror signals it has no stored files for this PR (pending backfill, in-flight
    # file job, etc.) — skip the round trip unless the repo explicitly supplies
    # a fixed base score.
    if not pr.scoring_data_stored and not has_fixed_base:
        return

    # Fetch file contents via the mirror's lazy /pulls/.../files endpoint.
    if pr.scoring_data_stored:
        try:
            files = (await asyncio.to_thread(client.get_pr_files, pr.repo_full_name, pr.pr_number)).files
        except MirrorRequestError as e:
            bt.logging.warning(f'Mirror file fetch failed for PR #{pr.pr_number}: {e}')
            if not has_fixed_base:
                return
            files = []
        scored.files = files

        if files:
            file_changes, file_contents = mirror_files_to_legacy(pr.repo_full_name, pr.pr_number, files)

            result = calculate_base_score_for_pr_files(
                file_changes,
                file_contents,
                programming_languages,
                token_config,
                src_tok_saturation_scale=scoring_cfg.src_tok_saturation_scale,
            )
            scored.token_score = result.token_score
            scored.structural_count = result.structural_count
            scored.structural_score = result.structural_score
            scored.leaf_count = result.leaf_count
            scored.leaf_score = result.leaf_score
            scored.total_nodes_scored = result.total_nodes_scored
            scored.base_score = result.base_score
        elif not has_fixed_base:
            bt.logging.warning(f'No files returned for PR #{pr.pr_number}')
            return

    if repo_config.fixed_base_score is not None:
        # Only the base score is overridden. Token fields stay token-derived so
        # eligibility and reporting gates keep their evidence signal.
        scored.base_score = repo_config.fixed_base_score

    _calculate_pr_multipliers(scored, repo_config, scoring_cfg)

    if pr.state == 'MERGED':
        eval_.unique_repos_contributed_to.add(pr.repo_full_name)
        # Token totals are aggregated later in finalize_miner_scores; this
        # function only sets per-PR state.


# ============================================================================
# Eligibility gate (MERGED PRs)
# ============================================================================


def _should_skip_merged_mirror_pr(scored: ScoredPR, repo_config: RepositoryConfig) -> Tuple[bool, Optional[str]]:
    """Eligibility gate for MERGED PRs.

    Checks:
    - mergedAt presence (mirror always sets this for MERGED PRs but verify defensively)
    - Author not a maintainer (already filtered at load time, but recheck for safety)
    - Self-merge: skip unless review_summary.approved_count > 0
      (GitHub forbids self-approval, so any approval count > 0 implies external approval)
    - base_ref check: if an acceptable set can be built (default_branch and/or
      additional_acceptable_branches), reject PRs whose base_ref doesn't match it.
      Supports wildcards via ``branch_matches_pattern`` (e.g. ``*-dev``).
    - head_ref check: reject PRs whose source branch is itself in the acceptable
      set (blocks e.g. ``staging -> main`` when both acceptable). Only applies
      to same-repo PRs — fork branch names are arbitrary.

    Note on ``edited_after_merge``: gates only the issue bonus
    (see ``_is_valid_linked_issue``), not the whole PR — a post-merge body/title
    edit invalidates the issue multiplier but the PR's base score and other
    multipliers still apply.

    When the mirror response is missing a field (older data predating the
    schema additions), some checks fall through rather than false-positive-
    blocking. Concretely: missing ``head_ref`` or ``head_repo_full_name`` skips
    the head_ref check. Missing ``default_branch`` falls back to ``main``.
    """
    pr = scored.pr

    if pr.merged_at is None:
        return True, f'PR #{pr.pr_number} is MERGED but missing merged_at'

    # Defensive recheck — load already drops these (with DEV_MODE bypass)
    if not os.environ.get('DEV_MODE') and pr.author_association in MAINTAINER_ASSOCIATIONS:
        return True, f'PR #{pr.pr_number} author is {pr.author_association}'

    if pr.merged_by_login and pr.merged_by_login.lower() == pr.author_login.lower():
        if pr.review_summary.approved_count == 0:
            return True, f'PR #{pr.pr_number} self-merged without external approval'

    additional = repo_config.additional_acceptable_branches or []
    default_branch = pr.default_branch or 'main'
    acceptable = [default_branch] + additional

    # base_ref check.
    if not branch_matches_pattern(pr.base_ref or '', acceptable):
        return True, (f'PR #{pr.pr_number} merged to {pr.base_ref!r} not in acceptable branches={acceptable}')

    # head_ref check — block PRs whose source branch is itself an acceptable
    # branch. Only applies to same-repo PRs: fork branch names are arbitrary.
    # Falls through when head_ref or head_repo_full_name is missing (older data).
    is_same_repo = pr.head_repo_full_name is not None and pr.head_repo_full_name == pr.repo_full_name
    if acceptable and pr.head_ref and is_same_repo and branch_matches_pattern(pr.head_ref, acceptable):
        return True, (
            f'PR #{pr.pr_number} source branch {pr.head_ref!r} is itself in '
            f'acceptable branches — merging between acceptable branches not allowed'
        )

    return False, None


# ============================================================================
# Base score
# ============================================================================


@dataclass
class BaseScoreResult:
    """Result of computing the base score for a PR's file diff.

    Used by both the OSS scoring path (to populate ``ScoredPR`` fields)
    and the issue discovery path (to produce ``discovery_base_score`` for a
    solving PR that wasn't scored by OSS, typically a non-miner solving PR).
    """

    base_score: float
    token_score: float
    structural_count: int
    structural_score: float
    leaf_count: int
    leaf_score: float
    total_nodes_scored: int


def calculate_base_score_for_pr_files(
    file_changes: List[FileChange],
    file_contents: Dict[str, FileContentPair],
    programming_languages: Dict[str, LanguageConfig],
    token_config: TokenConfig,
    src_tok_saturation_scale: Optional[float] = None,
) -> BaseScoreResult:
    """Saturation-curve base score on source token score, plus contribution bonus.

    ``src_tok_saturation_scale`` defaults to the global ``SRC_TOK_SATURATION_SCALE``;
    callers should pass the resolved per-repo value from ``resolve_scoring(...)``.
    """
    scale = src_tok_saturation_scale if src_tok_saturation_scale is not None else SRC_TOK_SATURATION_SCALE
    scoring_result: PrScoringResult = calculate_token_score_from_file_changes(
        file_changes,
        file_contents,
        token_config,
        programming_languages,
    )

    if scoring_result.score_breakdown:
        token_score = scoring_result.score_breakdown.total_score
        structural_count = scoring_result.score_breakdown.structural_count
        structural_score = scoring_result.score_breakdown.structural_score
        leaf_count = scoring_result.score_breakdown.leaf_count
        leaf_score = scoring_result.score_breakdown.leaf_score
        total_nodes_scored = structural_count + leaf_count
    else:
        token_score = 0.0
        structural_count = 0
        structural_score = 0.0
        leaf_count = 0
        leaf_score = 0.0
        total_nodes_scored = 0

    source = scoring_result.by_category.get(ScoringCategory.SOURCE)
    source_token_score = source.score_breakdown.total_score if source and source.score_breakdown else 0.0

    initial_base_score = MERGED_PR_BASE_SCORE * (1.0 - math.exp(-source_token_score / scale))

    bonus_percent = min(1.0, scoring_result.total_score / CONTRIBUTION_SCORE_FOR_FULL_BONUS)
    contribution_bonus = round(bonus_percent * MAX_CONTRIBUTION_BONUS, 2)
    base_score = round(initial_base_score + contribution_bonus, 2)

    bt.logging.info(
        f'Base score: {initial_base_score:.2f} '
        f'(src_tok {source_token_score:.1f}, scale {scale:.1f})'
        f' + {contribution_bonus} bonus ({bonus_percent * 100:.0f}% of max {MAX_CONTRIBUTION_BONUS})'
        f' = {base_score:.2f}'
    )

    return BaseScoreResult(
        base_score=base_score,
        token_score=token_score,
        structural_count=structural_count,
        structural_score=structural_score,
        leaf_count=leaf_count,
        leaf_score=leaf_score,
        total_nodes_scored=total_nodes_scored,
    )


# ============================================================================
# Per-PR multipliers
# ============================================================================


def _calculate_pr_multipliers(scored: ScoredPR, repo_config: RepositoryConfig, scoring_cfg: ResolvedScoring) -> None:
    """Compute time_decay, review_quality, label, and issue multipliers.

    Spam and credibility multipliers are deferred to ``finalize_miner_scores``
    — they depend on per-miner aggregate counts.
    """
    pr = scored.pr
    is_merged = pr.state == 'MERGED'

    chosen_label, label_multiplier = _resolve_trusted_scoring_label(pr, repo_config)
    scored.label = chosen_label
    scored.label_multiplier = label_multiplier

    scored.issue_multiplier = round(_calculate_issue_multiplier(scored, scoring_cfg), 2)

    if is_merged:
        assert pr.merged_at is not None, f'MERGED PR #{pr.pr_number} missing merged_at'
        scored.open_pr_spam_multiplier = 1.0  # finalized later with combined open-PR count
        scored.time_decay_multiplier = round(calculate_time_decay(pr.merged_at, scoring_cfg.time_decay), 2)
        scored.review_quality_multiplier = round(
            calculate_review_quality_multiplier(
                pr.review_summary.maintainer_changes_requested_count,
                scoring_cfg.review_penalty_rate,
                pr.pr_number,
            ),
            2,
        )
    else:
        scored.open_pr_spam_multiplier = 1.0
        scored.time_decay_multiplier = 1.0
        scored.credibility_multiplier = 1.0
        scored.review_quality_multiplier = 1.0


def _resolve_trusted_scoring_label(pr: MirrorPullRequest, repo_config: RepositoryConfig) -> tuple[Optional[str], float]:
    return resolve_trusted_label_multiplier(pr.labels, repo_config)


# ============================================================================
# Issue multiplier (uses inline linked_issues)
# ============================================================================


def _calculate_issue_multiplier(scored: ScoredPR, scoring: ResolvedScoring) -> float:
    """Return the multiplier earned from valid linked issues on a PR.

    Maintainer-authored valid issues bump the multiplier higher
    (``maintainer_issue_multiplier`` vs ``standard_issue_multiplier``).
    Returns 1.0 if no linked issues pass the anti-gaming gates.
    """
    pr = scored.pr
    if not pr.linked_issues:
        bt.logging.info(f'PR #{pr.pr_number} - Contains no linked issues')
        return 1.0

    valid = [li for li in pr.linked_issues if _is_valid_linked_issue(li, pr)]
    if not valid:
        bt.logging.info(f'PR #{pr.pr_number} - Solved no valid linked issues')
        return 1.0

    # Prefer a maintainer-authored valid issue so the multiplier doesn't depend
    # on mirror response ordering of linked_issues (regression seen in PR #673).
    issue = next(
        (li for li in valid if li.author_association in MAINTAINER_ASSOCIATIONS),
        valid[0],
    )
    is_maintainer = issue.author_association in MAINTAINER_ASSOCIATIONS if issue.author_association else False
    multiplier = scoring.maintainer_issue_multiplier if is_maintainer else scoring.standard_issue_multiplier
    label = 'maintainer' if is_maintainer else 'standard'
    bt.logging.info(f'Linked issue #{issue.number} - {label} | multiplier: {multiplier}')
    return multiplier


def _is_valid_linked_issue(li: MirrorLinkedIssue, pr: MirrorPullRequest) -> bool:
    """Anti-gaming gates for issue → PR multiplier credit.

    - Reject transferred issues.
    - Missing author / self-issue (uses github_id for immutability).
    - Issue created after the PR.
    - Any CLOSED issue must have state_reason=COMPLETED — NOT_PLANNED / reopened
      closures never grant a multiplier. Applies regardless of PR state, so the
      gate covers OPEN-PR collateral as well.
    - Mirror solver attribution: when ``li.solved_by_pr`` is populated, the
      scored PR must be that solver. Mirrors the issue-discovery path, which
      already treats ``solved_by_pr`` as authoritative. Fails open on ``None``
      to preserve behavior on older snapshots that don't set the field.
    - Additional MERGED-PR-only gates: edited_after_merge, issue must be CLOSED,
      close-timing window vs. merge (rejects both too-far-after AND too-far-before
      — negative days means the issue closed before the PR merged, so the PR
      can't have been the solver).
    """
    if li.is_transferred:
        bt.logging.warning(f'Skipping linked issue #{li.number} - transferred')
        return False

    if li.author_github_id is None:
        bt.logging.warning(f'Skipping linked issue #{li.number} - missing author_github_id')
        return False

    if li.author_github_id == pr.author_github_id:
        bt.logging.warning(f'Skipping linked issue #{li.number} - same author as PR (self-issue)')
        return False

    if li.created_at and li.created_at > pr.created_at:
        bt.logging.warning(f'Skipping linked issue #{li.number} - created after PR')
        return False

    # state_reason check applies regardless of PR state — OPEN-PR collateral
    # also requires that any CLOSED linked issue closed as COMPLETED.
    if li.state == 'CLOSED' and li.state_reason != 'COMPLETED':
        bt.logging.warning(f'Skipping linked issue #{li.number} - state_reason={li.state_reason} (need COMPLETED)')
        return False

    if li.solved_by_pr is not None and li.solved_by_pr != pr.pr_number:
        bt.logging.warning(
            f'Skipping linked issue #{li.number} - solved_by_pr={li.solved_by_pr} '
            f'!= PR #{pr.pr_number} (different PR was the actual solver)'
        )
        return False

    is_merged = pr.state == 'MERGED'
    if is_merged and pr.merged_at:
        if pr.edited_after_merge:
            bt.logging.warning(f'Skipping linked issue #{li.number} - PR edited after merge')
            return False

        if li.state != 'CLOSED':
            bt.logging.warning(f'Skipping linked issue #{li.number} - state {li.state} (need CLOSED)')
            return False

        if li.closed_at:
            # Signed days diff. Negative means the issue was closed BEFORE the
            # PR merged — in that case the PR cannot be what closed the issue,
            # so it's not a valid solver.
            days_diff = (li.closed_at - pr.merged_at).total_seconds() / SECONDS_PER_DAY
            if days_diff > MAX_ISSUE_CLOSE_WINDOW_DAYS or days_diff < 0:
                bt.logging.warning(
                    f'Skipping linked issue #{li.number} - closed {days_diff:+.2f}d from merge '
                    f'(max {MAX_ISSUE_CLOSE_WINDOW_DAYS})'
                )
                return False

    return True
