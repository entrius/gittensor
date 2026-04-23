"""Adapters from mirror response types into legacy ``FileChange`` / ``Issue``.

Two boundaries need this:
- Token-scoring infra in ``gittensor.validator.utils.tree_sitter_scoring`` expects
  ``List[FileChange]`` + ``Dict[str, FileContentPair]`` — see
  ``score_mirror_miner_prs``.
- Storage layer (``MinerEvaluation.get_all_file_changes``,
  ``MinerEvaluation.get_all_issues``) writes per-PR rows to the analytics DB
  using the legacy types.

When the legacy types are retired on flip-day this whole module goes away.
"""

from typing import Dict, List, Tuple

from gittensor.classes import FileChange, Issue
from gittensor.utils.github_api_tools import FileContentPair
from gittensor.utils.mirror.models import MirrorFile, MirrorLinkedIssue


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


def mirror_linked_issue_to_legacy_issue(
    li: MirrorLinkedIssue, pr_number: int, repo_full_name: str
) -> Issue:
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
