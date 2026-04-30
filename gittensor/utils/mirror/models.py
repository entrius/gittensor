"""Response shapes for the das-github-mirror scoring API.

One dataclass per distinct JSON object in a mirror response. Each class owns a
``from_dict`` classmethod that parses the raw JSON. Timestamps come in as ISO
strings and are normalized to timezone-aware UTC ``datetime`` objects so the
rest of the validator can do direct datetime arithmetic.

Shapes track the brief at docs/mirror-integration.md and the endpoint responses
from https://mirror.gittensor.io/api/v1/*.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import bittensor as bt

from gittensor.validator.utils.datetime_utils import (
    parse_github_iso_to_utc,
    parse_optional_github_iso_to_utc,
)


def _parse_mirror_labels(raw_labels: list[dict], parent_identifier: str) -> list['MirrorLabel']:
    labels: list[MirrorLabel] = []
    for raw in raw_labels or []:
        try:
            labels.append(MirrorLabel.from_dict(raw))
        except Exception as e:
            name = raw.get('name', '?') if isinstance(raw, dict) else '?'
            bt.logging.warning(f'Skipping malformed mirror label {name} on {parent_identifier}: {e}')
    return labels


def _parse_mirror_linked_issues(raw_issues: list[dict], pr_identifier: str) -> list['MirrorLinkedIssue']:
    linked_issues: list[MirrorLinkedIssue] = []
    for raw in raw_issues or []:
        try:
            linked_issues.append(MirrorLinkedIssue.from_dict(raw))
        except Exception as e:
            identifier = raw.get('number', '?') if isinstance(raw, dict) else '?'
            bt.logging.warning(f'Skipping malformed mirror linked issue {identifier} on {pr_identifier}: {e}')
    return linked_issues


@dataclass
class MirrorLabel:
    """A label applied to a PR or issue, with the actor who applied it.

    actor fields are nullable because labels on backfilled data may not have
    actor attribution reconstructed from the timeline.
    """

    name: str
    actor_github_id: Optional[str] = None
    actor_association: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorLabel':
        return cls(
            name=data['name'],
            actor_github_id=data.get('actor_github_id'),
            actor_association=data.get('actor_association'),
        )


@dataclass
class MirrorReviewSummary:
    """Aggregated PR review counts. Only ``maintainer_changes_requested_count``
    is a scoring input today; the others are included for observability.

    On the inline ``solving_pr`` object attached to issues, only
    ``maintainer_changes_requested_count`` is populated — the others default to 0.
    """

    maintainer_changes_requested_count: int = 0
    changes_requested_count: int = 0
    approved_count: int = 0
    commented_count: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorReviewSummary':
        if 'maintainer_changes_requested_count' not in data:
            bt.logging.warning(
                'MirrorReviewSummary missing maintainer_changes_requested_count — '
                'defaulting to 0 (review penalty/clean-bonus may be miscalculated)'
            )
        return cls(
            maintainer_changes_requested_count=data.get('maintainer_changes_requested_count', 0),
            changes_requested_count=data.get('changes_requested_count', 0),
            approved_count=data.get('approved_count', 0),
            commented_count=data.get('commented_count', 0),
        )


@dataclass
class MirrorLinkedIssue:
    """Issue that a PR closes, as nested inside a ``MirrorPullRequest``."""

    number: int
    title: str
    state: str
    state_reason: Optional[str]
    author_github_id: Optional[str]
    author_association: Optional[str]
    created_at: Optional[datetime]
    closed_at: Optional[datetime]
    updated_at: Optional[datetime]
    is_transferred: bool
    solved_by_pr: Optional[int]
    labels: List[MirrorLabel] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorLinkedIssue':
        # Mirror sometimes serializes github_id as int; coerce to match the
        # str-typed field so downstream `==` comparisons with author_github_id
        # from MirrorPullRequest don't silently mismatch on type.
        author_github_id = data.get('author_github_id')
        issue_identifier = f'linked issue {data.get("number", "?")}'
        return cls(
            number=data['number'],
            title=data.get('title', ''),
            state=data['state'],
            state_reason=data.get('state_reason'),
            author_github_id=str(author_github_id) if author_github_id is not None else None,
            author_association=data.get('author_association'),
            created_at=parse_optional_github_iso_to_utc(data.get('created_at')),
            closed_at=parse_optional_github_iso_to_utc(data.get('closed_at')),
            updated_at=parse_optional_github_iso_to_utc(data.get('updated_at')),
            is_transferred=bool(data.get('is_transferred', False)),
            solved_by_pr=data.get('solved_by_pr'),
            labels=_parse_mirror_labels(data.get('labels') or [], issue_identifier),
        )


@dataclass
class MirrorPullRequest:
    """One PR bundle from ``/api/v1/miners/:github_id/pulls``.

    All scoring inputs are inlined: review counts, labels with actor attribution,
    and linked issues. File contents are fetched separately via
    ``MirrorClient.get_pr_files``.
    """

    repo_full_name: str
    pr_number: int
    title: str
    body: Optional[str]
    state: str
    author_github_id: str
    author_login: str
    author_association: Optional[str]
    created_at: datetime
    closed_at: Optional[datetime]
    merged_at: Optional[datetime]
    last_edited_at: Optional[datetime]
    edited_after_merge: bool
    hours_since_merge: Optional[float]
    merged_by_login: Optional[str]
    base_ref: Optional[str]
    head_ref: Optional[str]
    head_repo_full_name: Optional[str]
    default_branch: Optional[str]
    head_sha: Optional[str]
    base_sha: Optional[str]
    merge_base_sha: Optional[str]
    additions: int
    deletions: int
    commits_count: int
    scoring_data_stored: bool
    review_summary: MirrorReviewSummary
    labels: List[MirrorLabel] = field(default_factory=list)
    linked_issues: List[MirrorLinkedIssue] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorPullRequest':
        # Repo names normalized to lowercase here so every downstream comparison
        # (mirror_repos lookups, head==base same-repo check, unique-repo set
        # union with the legacy lowercased path) is case-correct without each
        # call site re-applying .lower().
        head_repo = data.get('head_repo_full_name')
        pr_identifier = f'{data.get("repo_full_name", "?")}#{data.get("pr_number", "?")}'
        return cls(
            repo_full_name=data['repo_full_name'].lower(),
            pr_number=data['pr_number'],
            title=data.get('title', ''),
            body=data.get('body'),
            state=data['state'],
            author_github_id=str(data['author_github_id']),
            author_login=data.get('author_login', ''),
            author_association=data.get('author_association'),
            created_at=parse_github_iso_to_utc(data['created_at']),
            closed_at=parse_optional_github_iso_to_utc(data.get('closed_at')),
            merged_at=parse_optional_github_iso_to_utc(data.get('merged_at')),
            last_edited_at=parse_optional_github_iso_to_utc(data.get('last_edited_at')),
            edited_after_merge=bool(data.get('edited_after_merge', False)),
            hours_since_merge=data.get('hours_since_merge'),
            merged_by_login=data.get('merged_by_login'),
            base_ref=data.get('base_ref'),
            head_ref=data.get('head_ref'),
            head_repo_full_name=head_repo.lower() if head_repo else None,
            default_branch=data.get('default_branch'),
            head_sha=data.get('head_sha'),
            base_sha=data.get('base_sha'),
            merge_base_sha=data.get('merge_base_sha'),
            additions=int(data.get('additions', 0)),
            deletions=int(data.get('deletions', 0)),
            commits_count=int(data.get('commits_count', 0)),
            scoring_data_stored=bool(data.get('scoring_data_stored', False)),
            review_summary=MirrorReviewSummary.from_dict(data.get('review_summary') or {}),
            labels=_parse_mirror_labels(data.get('labels') or [], pr_identifier),
            linked_issues=_parse_mirror_linked_issues(data.get('linked_issues') or [], pr_identifier),
        )


@dataclass
class MirrorSolvingPR:
    """Minimal PR shape inlined inside ``MirrorIssue.solving_pr``.

    Lets validators score issues solved by non-miners without making a second
    request to fetch the solving PR's full bundle.
    """

    pr_number: int
    author_github_id: str
    state: str
    merged_at: Optional[datetime]
    hours_since_merge: Optional[float]
    edited_after_merge: bool
    head_sha: Optional[str]
    base_sha: Optional[str]
    merge_base_sha: Optional[str]
    review_summary: MirrorReviewSummary
    labels: List[MirrorLabel] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorSolvingPR':
        return cls(
            pr_number=data['pr_number'],
            author_github_id=str(data['author_github_id']),
            state=data['state'],
            merged_at=parse_optional_github_iso_to_utc(data.get('merged_at')),
            hours_since_merge=data.get('hours_since_merge'),
            edited_after_merge=bool(data.get('edited_after_merge', False)),
            head_sha=data.get('head_sha'),
            base_sha=data.get('base_sha'),
            merge_base_sha=data.get('merge_base_sha'),
            review_summary=MirrorReviewSummary.from_dict(data.get('review_summary') or {}),
            labels=[MirrorLabel.from_dict(label) for label in data.get('labels') or []],
        )


@dataclass
class MirrorIssue:
    """One issue bundle from ``/api/v1/miners/:github_id/issues``.

    ``solving_pr`` is populated whenever ``solved_by_pr`` is set; it carries the
    minimum fields needed to score the solving PR even if its author is not the
    miner whose issues were requested.
    """

    repo_full_name: str
    issue_number: int
    title: str
    state: str
    state_reason: Optional[str]
    author_github_id: Optional[str]
    author_login: Optional[str]
    author_association: Optional[str]
    created_at: Optional[datetime]
    closed_at: Optional[datetime]
    updated_at: Optional[datetime]
    last_edited_at: Optional[datetime]
    is_transferred: bool
    solved_by_pr: Optional[int]
    labels: List[MirrorLabel] = field(default_factory=list)
    solving_pr: Optional[MirrorSolvingPR] = None

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorIssue':
        solving_pr_raw = data.get('solving_pr')
        author_github_id = data.get('author_github_id')
        return cls(
            repo_full_name=data['repo_full_name'].lower(),
            issue_number=data['issue_number'],
            title=data.get('title', ''),
            state=data['state'],
            state_reason=data.get('state_reason'),
            author_github_id=str(author_github_id) if author_github_id is not None else None,
            author_login=data.get('author_login'),
            author_association=data.get('author_association'),
            created_at=parse_optional_github_iso_to_utc(data.get('created_at')),
            closed_at=parse_optional_github_iso_to_utc(data.get('closed_at')),
            updated_at=parse_optional_github_iso_to_utc(data.get('updated_at')),
            last_edited_at=parse_optional_github_iso_to_utc(data.get('last_edited_at')),
            is_transferred=bool(data.get('is_transferred', False)),
            solved_by_pr=data.get('solved_by_pr'),
            labels=[MirrorLabel.from_dict(label) for label in data.get('labels') or []],
            solving_pr=MirrorSolvingPR.from_dict(solving_pr_raw) if solving_pr_raw else None,
        )


@dataclass
class MirrorFile:
    """One file entry from ``/api/v1/pulls/:owner/:repo/:number/files``.

    ``head_content`` is null for binaries, files over 1 MB, and removed files.
    ``base_content`` is null for added files. Content is taken at
    ``merge_base_sha`` (true common ancestor) when available, else ``base_sha``.
    """

    filename: str
    previous_filename: Optional[str]
    status: str
    additions: int
    deletions: int
    changes: int
    is_binary: bool
    head_content: Optional[str]
    base_content: Optional[str]

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorFile':
        return cls(
            filename=data['filename'],
            previous_filename=data.get('previous_filename'),
            status=data['status'],
            additions=int(data.get('additions', 0)),
            deletions=int(data.get('deletions', 0)),
            changes=int(data.get('changes', 0)),
            is_binary=bool(data.get('is_binary', False)),
            head_content=data.get('head_content'),
            base_content=data.get('base_content'),
        )


@dataclass
class MirrorPullRequestsResponse:
    """Top-level wrapper for ``GET /api/v1/miners/:github_id/pulls``."""

    github_id: str
    since: Optional[datetime]
    generated_at: Optional[datetime]
    pull_requests: List[MirrorPullRequest] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorPullRequestsResponse':
        pull_requests: List[MirrorPullRequest] = []
        for raw in data.get('pull_requests') or []:
            try:
                pull_requests.append(MirrorPullRequest.from_dict(raw))
            except Exception as e:
                identifier = (
                    f'{raw.get("repo_full_name", "?")}#{raw.get("pr_number", "?")}' if isinstance(raw, dict) else '?'
                )
                bt.logging.warning(f'Skipping malformed mirror PR {identifier}: {e}')
        return cls(
            github_id=str(data['github_id']),
            since=parse_optional_github_iso_to_utc(data.get('since')),
            generated_at=parse_optional_github_iso_to_utc(data.get('generated_at')),
            pull_requests=pull_requests,
        )


@dataclass
class MirrorIssuesResponse:
    """Top-level wrapper for ``GET /api/v1/miners/:github_id/issues``."""

    github_id: str
    since: Optional[datetime]
    generated_at: Optional[datetime]
    issues: List[MirrorIssue] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorIssuesResponse':
        issues: List[MirrorIssue] = []
        for raw in data.get('issues') or []:
            try:
                issues.append(MirrorIssue.from_dict(raw))
            except Exception as e:
                identifier = (
                    f'{raw.get("repo_full_name", "?")}#{raw.get("issue_number", "?")}' if isinstance(raw, dict) else '?'
                )
                bt.logging.warning(f'Skipping malformed mirror issue {identifier}: {e}')
        return cls(
            github_id=str(data['github_id']),
            since=parse_optional_github_iso_to_utc(data.get('since')),
            generated_at=parse_optional_github_iso_to_utc(data.get('generated_at')),
            issues=issues,
        )


@dataclass
class MirrorPullRequestFilesResponse:
    """Top-level wrapper for ``GET /api/v1/pulls/:owner/:repo/:number/files``."""

    repo_full_name: str
    pr_number: int
    head_sha: Optional[str]
    base_sha: Optional[str]
    merge_base_sha: Optional[str]
    scoring_data_stored: bool
    files: List[MirrorFile] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorPullRequestFilesResponse':
        files: List[MirrorFile] = []
        for raw in data.get('files') or []:
            try:
                files.append(MirrorFile.from_dict(raw))
            except Exception as e:
                filename = raw.get('filename', '?') if isinstance(raw, dict) else '?'
                bt.logging.warning(f'Skipping malformed mirror file {filename}: {e}')
        return cls(
            repo_full_name=data['repo_full_name'].lower(),
            pr_number=int(data['pr_number']),
            head_sha=data.get('head_sha'),
            base_sha=data.get('base_sha'),
            merge_base_sha=data.get('merge_base_sha'),
            scoring_data_stored=bool(data.get('scoring_data_stored', False)),
            files=files,
        )
