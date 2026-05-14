"""Adapters from mirror response types into the storage-layer ``FileChange`` /
``Issue`` / ``PullRequest`` shapes.

Three boundaries need this:
- Token-scoring infra in ``gittensor.validator.utils.tree_sitter_scoring`` expects
  ``List[FileChange]`` + ``Dict[str, FileContentPair]`` — see
  ``score_miner_prs``.
- ``MinerEvaluation.get_all_file_changes`` / ``get_all_issues`` write per-PR
  rows to the analytics DB using these types.
- ``store_pull_requests_bulk`` writes per-PR rows for analytics; ScoredPR
  carries the same scoring fields as PullRequest but on a composition wrapper,
  so we flatten it into a PullRequest for that bulk insert.
"""

from typing import Dict, List, Optional, Tuple

from gittensor.classes import FileChange, Issue, PRState, PullRequest
from gittensor.utils.github_api_tools import FileContentPair
from gittensor.utils.mirror.models import MirrorFile, MirrorLinkedIssue
from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR


def mirror_files_to_legacy(
    repo_full_name: str,
    pr_number: int,
    files: List[MirrorFile],
) -> Tuple[List[FileChange], Dict[str, FileContentPair]]:
    """Translate MirrorFile entries into the legacy shapes token-scoring expects."""
    file_changes: List[FileChange] = []
    file_contents: Dict[str, FileContentPair] = {}

    for f in files:
        file_changes.append(
            FileChange(
                pr_number=pr_number,
                repository_full_name=repo_full_name,
                filename=f.filename,
                changes=f.changes,
                additions=f.additions,
                deletions=f.deletions,
                status=f.status,
                patch=None,  # mirror doesn't return patch text; tree-diff only needs head/base content
                previous_filename=f.previous_filename,
            )
        )
        file_contents[f.filename] = FileContentPair(
            old_content=f.base_content,
            new_content=f.head_content,
        )

    return file_changes, file_contents


def mirror_linked_issue_to_legacy_issue(li: MirrorLinkedIssue, pr_number: int, repo_full_name: str) -> Issue:
    """Adapt a MirrorLinkedIssue into a legacy Issue for storage.

    Mirror doesn't carry ``author_login`` on linked issues (only ``author_github_id``),
    so the resulting Issue has ``author_login=None``. Storage code should not rely
    on author_login being set for mirror-sourced issues.

    ``body_or_title_edited_at`` is left None — mirror doesn't expose a reliable
    body/title edit timestamp for linked issues (``updated_at`` is noisy).
    """
    return Issue(
        number=li.number,
        pr_number=pr_number,
        repository_full_name=repo_full_name,
        title=li.title,
        created_at=li.created_at,
        closed_at=li.closed_at,
        author_login=None,
        state=li.state,
        author_association=li.author_association,
        author_github_id=li.author_github_id,
        state_reason=li.state_reason,
        updated_at=li.updated_at,
        body_or_title_edited_at=None,
    )


def mirror_scored_pr_to_legacy_pull_request(
    scored: ScoredPR,
    uid: int,
    hotkey: str,
    github_id: Optional[str],
) -> PullRequest:
    """Adapt a ``ScoredPR`` into a legacy ``PullRequest`` for storage.

    ScoredPR carries identical scoring fields (multipliers, base_score,
    earned_score, token breakdown) but raw response data lives on the nested
    ``.pr`` attribute. uid / hotkey / github_id come from the parent
    ``MinerEvaluation`` since ScoredPR doesn't carry miner identity.

    Mirror has different field names for a few raw values:
    - ``pr.pr_number`` → ``PullRequest.number``
    - ``pr.repo_full_name`` → ``PullRequest.repository_full_name``
    - ``pr.commits_count`` → ``PullRequest.commits``
    - ``pr.body`` → ``PullRequest.description``
    - ``pr.head_sha`` → ``PullRequest.head_ref_oid``
    - ``pr.base_sha`` → ``PullRequest.base_ref_oid``
    - state string ``MERGED|OPEN|CLOSED`` → ``PRState`` enum

    ``file_changes`` and ``issues`` are left as None on the resulting PullRequest
    — those are written to the DB through ``get_all_file_changes`` / ``get_all_issues``
    which already adapt mirror data via the other helpers above.
    """
    pr = scored.pr
    return PullRequest(
        number=pr.pr_number,
        repository_full_name=pr.repo_full_name,
        uid=uid,
        hotkey=hotkey,
        github_id=github_id,
        title=pr.title,
        author_login=pr.author_login,
        merged_at=pr.merged_at,
        created_at=pr.created_at,
        pr_state=PRState(pr.state),
        base_score=scored.base_score,
        issue_multiplier=scored.issue_multiplier,
        open_pr_spam_multiplier=scored.open_pr_spam_multiplier,
        pioneer_dividend=scored.pioneer_dividend,
        pioneer_rank=scored.pioneer_rank,
        time_decay_multiplier=scored.time_decay_multiplier,
        credibility_multiplier=scored.credibility_multiplier,
        review_quality_multiplier=scored.review_quality_multiplier,
        label_multiplier=scored.label_multiplier,
        label=scored.label,
        # Mirror's review_summary surfaces the maintainer-only count directly;
        # legacy PullRequest.changes_requested_count holds the same scoring input.
        changes_requested_count=pr.review_summary.maintainer_changes_requested_count,
        earned_score=scored.earned_score,
        collateral_score=scored.collateral_score,
        additions=pr.additions,
        deletions=pr.deletions,
        commits=pr.commits_count,
        total_nodes_scored=scored.total_nodes_scored,
        code_density=scored.code_density,
        token_score=scored.token_score,
        structural_count=scored.structural_count,
        structural_score=scored.structural_score,
        leaf_count=scored.leaf_count,
        leaf_score=scored.leaf_score,
        merged_by_login=pr.merged_by_login,
        file_changes=None,  # written separately via get_all_file_changes
        issues=None,  # written separately via get_all_issues
        description=pr.body,
        last_edited_at=pr.last_edited_at,
        head_ref_oid=pr.head_sha,
        base_ref_oid=pr.base_sha,
    )
