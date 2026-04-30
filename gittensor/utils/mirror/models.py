"""Response shapes for the das-github-mirror scoring API.

One ``msgspec.Struct`` per JSON object in a mirror response. Each class owns a
``from_dict`` classmethod that parses the raw JSON. Timestamps come in as ISO
strings and are normalized to timezone-aware UTC ``datetime`` objects so the
rest of the validator can do direct datetime arithmetic.

Shapes track the brief at docs/mirror-integration.md and the endpoint responses
from https://mirror.gittensor.io/api/v1/*.
"""

from datetime import datetime
from typing import Callable, List, Optional, Type, TypeVar

import bittensor as bt
import msgspec

T = TypeVar('T', bound=msgspec.Struct)

# Mirror sometimes ships these id fields as int; coerce at the boundary so all
# downstream `==` comparisons hold and the Struct field types stay narrow `str`
_ID_FIELDS = frozenset({'author_github_id', 'actor_github_id', 'github_id'})


def _coerce_id_fields(obj: object) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _ID_FIELDS and isinstance(v, int):
                obj[k] = str(v)
            elif isinstance(v, (dict, list)):
                _coerce_id_fields(v)
    elif isinstance(obj, list):
        for item in obj:
            _coerce_id_fields(item)


class MirrorLabel(msgspec.Struct):
    """A label applied to a PR or issue, with the actor who applied it.

    actor fields are nullable because labels on backfilled data may not have
    actor attribution reconstructed from the timeline.
    """

    name: str
    actor_github_id: Optional[str] = None
    actor_association: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorLabel':
        return msgspec.convert(data, cls)


class MirrorReviewSummary(msgspec.Struct):
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
        return msgspec.convert(data, cls)


class MirrorLinkedIssue(msgspec.Struct):
    """Issue that a PR closes, as nested inside a ``MirrorPullRequest``."""

    number: int
    state: str
    title: str = ''
    state_reason: Optional[str] = None
    author_github_id: Optional[str] = None
    author_association: Optional[str] = None
    created_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    is_transferred: bool = False
    solved_by_pr: Optional[int] = None
    labels: List[MirrorLabel] = []

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorLinkedIssue':
        _coerce_id_fields(data)
        return msgspec.convert(data, cls)


class MirrorPullRequest(msgspec.Struct):
    """One PR bundle from ``/api/v1/miners/:github_id/pulls``.

    All scoring inputs are inlined: review counts, labels with actor attribution,
    and linked issues. File contents are fetched separately via
    ``MirrorClient.get_pr_files``.
    """

    repo_full_name: str
    pr_number: int
    state: str
    author_github_id: str
    created_at: datetime
    title: str = ''
    body: Optional[str] = None
    author_login: str = ''
    author_association: Optional[str] = None
    closed_at: Optional[datetime] = None
    merged_at: Optional[datetime] = None
    last_edited_at: Optional[datetime] = None
    edited_after_merge: bool = False
    hours_since_merge: Optional[float] = None
    merged_by_login: Optional[str] = None
    base_ref: Optional[str] = None
    head_ref: Optional[str] = None
    head_repo_full_name: Optional[str] = None
    default_branch: Optional[str] = None
    head_sha: Optional[str] = None
    base_sha: Optional[str] = None
    merge_base_sha: Optional[str] = None
    additions: int = 0
    deletions: int = 0
    commits_count: int = 0
    scoring_data_stored: bool = False
    review_summary: MirrorReviewSummary = msgspec.field(default_factory=MirrorReviewSummary)
    labels: List[MirrorLabel] = []
    linked_issues: List[MirrorLinkedIssue] = []

    def __post_init__(self) -> None:
        # Lowercase at the boundary so downstream mirror_repos lookups need no per-site .lower()
        self.repo_full_name = self.repo_full_name.lower()
        if self.head_repo_full_name:
            self.head_repo_full_name = self.head_repo_full_name.lower()

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorPullRequest':
        _coerce_id_fields(data)
        return msgspec.convert(data, cls)


class MirrorSolvingPR(msgspec.Struct):
    """Minimal PR shape inlined inside ``MirrorIssue.solving_pr``.

    Lets validators score issues solved by non-miners without making a second
    request to fetch the solving PR's full bundle.
    """

    pr_number: int
    author_github_id: str
    state: str
    merged_at: Optional[datetime] = None
    hours_since_merge: Optional[float] = None
    edited_after_merge: bool = False
    head_sha: Optional[str] = None
    base_sha: Optional[str] = None
    merge_base_sha: Optional[str] = None
    review_summary: MirrorReviewSummary = msgspec.field(default_factory=MirrorReviewSummary)
    labels: List[MirrorLabel] = []

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorSolvingPR':
        _coerce_id_fields(data)
        return msgspec.convert(data, cls)


class MirrorIssue(msgspec.Struct):
    """One issue bundle from ``/api/v1/miners/:github_id/issues``.

    ``solving_pr`` is populated whenever ``solved_by_pr`` is set; it carries the
    minimum fields needed to score the solving PR even if its author is not the
    miner whose issues were requested.
    """

    repo_full_name: str
    issue_number: int
    state: str
    title: str = ''
    state_reason: Optional[str] = None
    author_github_id: Optional[str] = None
    author_login: Optional[str] = None
    author_association: Optional[str] = None
    created_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_edited_at: Optional[datetime] = None
    is_transferred: bool = False
    solved_by_pr: Optional[int] = None
    labels: List[MirrorLabel] = []
    solving_pr: Optional[MirrorSolvingPR] = None

    def __post_init__(self) -> None:
        self.repo_full_name = self.repo_full_name.lower()

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorIssue':
        _coerce_id_fields(data)
        return msgspec.convert(data, cls)


class MirrorFile(msgspec.Struct):
    """One file entry from ``/api/v1/pulls/:owner/:repo/:number/files``.

    ``head_content`` is null for binaries, files over 1 MB, and removed files.
    ``base_content`` is null for added files. Content is taken at
    ``merge_base_sha`` (true common ancestor) when available, else ``base_sha``.
    """

    filename: str
    status: str
    previous_filename: Optional[str] = None
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    is_binary: bool = False
    head_content: Optional[str] = None
    base_content: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorFile':
        return msgspec.convert(data, cls)


def _decode_items_skip_malformed(
    raw_items: List[dict],
    item_cls: Type[T],
    identify: Callable[[dict], str],
    kind: str,
) -> List[T]:
    """Decode items individually; malformed ones are logged and skipped"""
    parsed: List[T] = []
    for raw in raw_items:
        try:
            parsed.append(msgspec.convert(raw, item_cls))
        except (msgspec.ValidationError, ValueError, TypeError, KeyError) as e:
            ident: str = identify(raw) if isinstance(raw, dict) else '?'
            bt.logging.warning(f'Skipping malformed mirror {kind} {ident}: {e}')
    return parsed


class MirrorPullRequestsResponse(msgspec.Struct):
    """Top-level wrapper for ``GET /api/v1/miners/:github_id/pulls``."""

    github_id: str
    since: Optional[datetime] = None
    generated_at: Optional[datetime] = None
    pull_requests: List[MirrorPullRequest] = []

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorPullRequestsResponse':
        _coerce_id_fields(data)
        prs = _decode_items_skip_malformed(
            data.get('pull_requests') or [],
            MirrorPullRequest,
            lambda raw: f'{raw.get("repo_full_name", "?")}#{raw.get("pr_number", "?")}',
            'PR',
        )
        return msgspec.convert({**data, 'pull_requests': prs}, cls)


class MirrorIssuesResponse(msgspec.Struct):
    """Top-level wrapper for ``GET /api/v1/miners/:github_id/issues``."""

    github_id: str
    since: Optional[datetime] = None
    generated_at: Optional[datetime] = None
    issues: List[MirrorIssue] = []

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorIssuesResponse':
        _coerce_id_fields(data)
        issues = _decode_items_skip_malformed(
            data.get('issues') or [],
            MirrorIssue,
            lambda raw: f'{raw.get("repo_full_name", "?")}#{raw.get("issue_number", "?")}',
            'issue',
        )
        return msgspec.convert({**data, 'issues': issues}, cls)


class MirrorPullRequestFilesResponse(msgspec.Struct):
    """Top-level wrapper for ``GET /api/v1/pulls/:owner/:repo/:number/files``."""

    repo_full_name: str
    pr_number: int
    head_sha: Optional[str] = None
    base_sha: Optional[str] = None
    merge_base_sha: Optional[str] = None
    scoring_data_stored: bool = False
    files: List[MirrorFile] = []

    def __post_init__(self) -> None:
        self.repo_full_name = self.repo_full_name.lower()

    @classmethod
    def from_dict(cls, data: dict) -> 'MirrorPullRequestFilesResponse':
        files = _decode_items_skip_malformed(
            data.get('files') or [],
            MirrorFile,
            lambda raw: raw.get('filename', '?'),
            'file',
        )
        return msgspec.convert({**data, 'files': files}, cls)
