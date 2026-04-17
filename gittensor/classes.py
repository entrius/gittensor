import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import prod
from typing import DefaultDict, Dict, List, Optional, Set

import bittensor as bt

from gittensor.constants import MAX_CODE_DENSITY_MULTIPLIER, MIN_TOKEN_SCORE_FOR_BASE_SCORE
from gittensor.utils.utils import parse_repo_name

GITHUB_DOMAIN = 'https://github.com/'


class PRState(Enum):
    """PR state for scoring"""

    MERGED = 'MERGED'
    OPEN = 'OPEN'
    CLOSED = 'CLOSED'


@dataclass
class Miner:
    """Miner identity"""

    uid: int
    hotkey: str
    github_id: str

    def __str__(self) -> str:
        return f'Miner(uid={self.uid}, hotkey={self.hotkey[:8]}..., github_id={self.github_id})'


@dataclass
class FileChange:
    """Represents a single file change in a PR"""

    pr_number: int
    repository_full_name: str
    filename: str
    changes: int
    additions: int
    deletions: int
    status: str  # "added", "modified", "removed", "renamed", etc.
    patch: Optional[str] = None  # The actual diff content
    file_extension: Optional[str] = None
    previous_filename: Optional[str] = None  # For renamed files

    @property
    def short_name(self) -> str:
        """Return only the base filename (strip directories)."""
        return self.filename.split('/')[-1]

    def __post_init__(self):
        if self.file_extension is None:
            self.file_extension = self._calculate_file_extension()

    def _calculate_file_extension(self) -> str:
        basename = self.filename.split('/')[-1]
        return basename.split('.')[-1].lower() if '.' in basename else ''

    def is_test_file(self) -> bool:
        filename_lower = self.filename.lower()
        basename = filename_lower.split('/')[-1]

        test_dir_patterns = [
            r'(^|/)tests?/',
            r'(^|/)__tests?__/',
        ]
        if any(re.search(pattern, filename_lower) for pattern in test_dir_patterns):
            return True

        test_patterns = [
            r'^test_',
            r'^spec_',
            r'_test\.[^.]+$',
            r'_tests\.[^.]+$',
            r'\.test\.[^.]+$',
            r'\.tests\.[^.]+$',
            r'\.spec\.[^.]+$',
            r'^test\.[^.]+$',
            r'^tests\.[^.]+$',
        ]

        return any(re.search(pattern, basename) for pattern in test_patterns)

    @classmethod
    def from_github_response(cls, pr_number: int, repository_full_name: str, file_diff: DefaultDict) -> 'FileChange':
        """Create FileChange from GitHub API response"""
        return cls(
            pr_number=pr_number,
            repository_full_name=repository_full_name,
            filename=file_diff['filename'],
            changes=file_diff['changes'],
            additions=file_diff['additions'],
            deletions=file_diff['deletions'],
            status=file_diff['status'],
            patch=file_diff.get('patch'),
            previous_filename=file_diff.get('previous_filename'),
        )


@dataclass
class Issue:
    """Represents an issue that belongs to a pull request"""

    number: int
    pr_number: int
    repository_full_name: str
    title: str
    created_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    author_login: Optional[str] = None
    state: Optional[str] = None  # "OPEN" or "CLOSED"
    author_association: Optional[str] = None  # e.g., "OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "NONE"

    # Issue discovery fields
    author_github_id: Optional[str] = None  # Issue author's GitHub user ID (for miner matching)
    is_transferred: bool = False
    updated_at: Optional[datetime] = None
    body_or_title_edited_at: Optional[datetime] = None
    discovery_base_score: float = 0.0
    discovery_earned_score: float = 0.0
    discovery_review_quality_multiplier: float = 1.0
    discovery_repo_weight_multiplier: float = 1.0
    discovery_time_decay_multiplier: float = 1.0
    discovery_credibility_multiplier: float = 1.0
    discovery_open_issue_spam_multiplier: float = 1.0


@dataclass
class PullRequest:
    """Represents a pull request with relevant metadata for scoring.

    Supports both MERGED PRs (earned scores) and OPEN PRs (collateral scores).
    """

    number: int
    repository_full_name: str
    uid: int
    hotkey: str
    github_id: Optional[str]
    title: str
    author_login: str
    merged_at: Optional[datetime]  # None for OPEN PRs
    created_at: datetime

    # PR state based fields
    pr_state: PRState

    # Score fields
    repo_weight_multiplier: float = 1.0
    base_score: float = 0.0
    issue_multiplier: float = 1.0
    open_pr_spam_multiplier: float = 1.0
    pioneer_dividend: float = 0.0  # Additive bonus for pioneering a repo
    pioneer_rank: int = 0  # 0 = not eligible, 1 = pioneer, 2+ = follower position
    time_decay_multiplier: float = 1.0
    credibility_multiplier: float = 1.0
    review_quality_multiplier: float = 1.0  # Penalty for CHANGES_REQUESTED reviews from maintainers
    label_multiplier: float = 1.0  # Multiplier based on PR label (matched via keyword substring search)
    label: Optional[str] = None  # Last label set on the PR
    changes_requested_count: int = 0  # Number of maintainer CHANGES_REQUESTED reviews
    earned_score: float = 0.0
    collateral_score: float = 0.0  # For OPEN PRs: potential_score * collateral_percent

    # Contribution details
    additions: int = 0
    deletions: int = 0
    commits: int = 0
    total_nodes_scored: int = 0  # Total AST nodes scored for this PR

    # Token scoring breakdown (after test weight applied)
    code_density: float = 0.0
    token_score: float = 0.0
    structural_count: int = 0
    structural_score: float = 0.0
    leaf_count: int = 0
    leaf_score: float = 0.0
    merged_by_login: Optional[str] = None
    file_changes: Optional[List[FileChange]] = None
    issues: Optional[List[Issue]] = None
    description: Optional[str] = None
    last_edited_at: Optional[datetime] = None
    head_ref_oid: Optional[str] = None
    base_ref_oid: Optional[str] = None

    def set_file_changes(self, file_changes: List[FileChange]) -> None:
        """Set the file changes for this pull request"""
        self.file_changes = file_changes

    def is_pioneer_eligible(self) -> bool:
        """Check if this PR qualifies for pioneer consideration.

        A PR is eligible if it is merged and meets the minimum token score quality gate.
        """
        return self.merged_at is not None and self.token_score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE

    def calculate_final_earned_score(self) -> float:
        """Combine base score with all multipliers. Pioneer dividend is added separately after."""
        multipliers = {
            'repo': self.repo_weight_multiplier,
            'issue': self.issue_multiplier,
            'label': self.label_multiplier,
            'spam': self.open_pr_spam_multiplier,
            'decay': self.time_decay_multiplier,
            'cred': self.credibility_multiplier,
            'review': self.review_quality_multiplier,
        }

        self.earned_score = self.base_score * prod(multipliers.values())

        mult_str = ' × '.join(f'{k}={v:.2f}' for k, v in multipliers.items())
        bt.logging.info(
            f'├─ {self.pr_state.value} PR #{self.number} ({self.repository_full_name}) → {self.earned_score:.2f}'
        )
        bt.logging.info(f'│  └─ {self.base_score:.2f} × {mult_str}')

        return self.earned_score

    @classmethod
    def from_graphql_response(cls, pr_data: dict, uid: int, hotkey: str, github_id: Optional[str]) -> 'PullRequest':
        """Create PullRequest from GraphQL API response for any PR state."""
        from gittensor.validator.utils.datetime_utils import parse_github_timestamp_to_cst

        repository_full_name = parse_repo_name(pr_data['repository'])
        pr_state = PRState(pr_data['state'])
        is_merged = pr_state == PRState.MERGED

        # Issue extraction - merged PRs only count closed issues
        raw_issues: List[Dict] = pr_data.get('closingIssuesReferences', {}).get('nodes', [])
        issues = []
        for issue in raw_issues:
            if is_merged and not (issue.get('closedAt') and issue.get('state') == 'CLOSED'):
                continue
            issue_author = issue.get('author') or {}
            author_db_id = issue_author.get('databaseId')

            body_edit_history = (issue.get('userContentEdits') or {}).get('nodes') or []
            latest_body_edit_timestamp = next(
                (edit.get('editedAt') for edit in body_edit_history if edit and edit.get('editedAt')),
                None,
            )
            latest_body_edit_at = (
                parse_github_timestamp_to_cst(latest_body_edit_timestamp) if latest_body_edit_timestamp else None
            )

            title_rename_events = (issue.get('timelineItems') or {}).get('nodes') or []
            latest_title_rename_timestamp = next(
                (rename.get('createdAt') for rename in title_rename_events if rename and rename.get('createdAt')),
                None,
            )
            latest_title_rename_at = (
                parse_github_timestamp_to_cst(latest_title_rename_timestamp) if latest_title_rename_timestamp else None
            )

            if latest_body_edit_at and latest_title_rename_at:
                body_or_title_edited_at = max(latest_body_edit_at, latest_title_rename_at)
            else:
                body_or_title_edited_at = latest_body_edit_at or latest_title_rename_at

            issues.append(
                Issue(
                    number=issue['number'],
                    pr_number=pr_data['number'],
                    repository_full_name=repository_full_name,
                    title=issue['title'],
                    created_at=parse_github_timestamp_to_cst(issue['createdAt']) if issue.get('createdAt') else None,
                    closed_at=parse_github_timestamp_to_cst(issue['closedAt']) if issue.get('closedAt') else None,
                    author_login=issue_author.get('login'),
                    state=issue.get('state'),
                    author_association=issue.get('authorAssociation'),
                    author_github_id=str(author_db_id) if author_db_id else None,
                    updated_at=parse_github_timestamp_to_cst(issue['updatedAt']) if issue.get('updatedAt') else None,
                    body_or_title_edited_at=body_or_title_edited_at,
                )
            )

        description: str = pr_data.get('bodyText', '')
        raw_edited_at = pr_data.get('lastEditedAt')
        last_edited_at = parse_github_timestamp_to_cst(raw_edited_at) if isinstance(raw_edited_at, str) else None
        merged_at = parse_github_timestamp_to_cst(pr_data['mergedAt']) if is_merged else None

        # Extract last label from timeline events
        timeline_nodes = pr_data.get('timelineItems', {}).get('nodes', [])
        label = timeline_nodes[0]['label']['name'].lower() if timeline_nodes else None

        return cls(
            number=pr_data['number'],
            repository_full_name=repository_full_name,
            uid=uid,
            hotkey=hotkey,
            github_id=github_id,
            title=pr_data['title'],
            author_login=pr_data['author']['login'],
            merged_at=merged_at,
            created_at=parse_github_timestamp_to_cst(pr_data['createdAt']),
            pr_state=pr_state,
            additions=pr_data['additions'],
            deletions=pr_data['deletions'],
            commits=pr_data.get('commits', {}).get('totalCount', 0),
            merged_by_login=pr_data.get('mergedBy', {}).get('login') if is_merged else None,
            issues=issues if issues else None,
            description=description,
            last_edited_at=last_edited_at,
            head_ref_oid=pr_data.get('headRefOid'),
            base_ref_oid=pr_data.get('baseRefOid'),
            label=label,
        )


@dataclass
class MinerEvaluation:
    uid: int
    hotkey: str
    github_id: Optional[str] = '0'  # will be 0 if miner failed
    github_pat: Optional[str] = None
    base_total_score: float = 0.0
    total_score: float = 0.0
    total_collateral_score: float = 0.0  # Collateral from open PRs
    total_nodes_scored: int = 0  # Total AST nodes scored across all PRs
    unique_repos_count: int = 0

    # Overall token scoring breakdown (aggregated across all PRs)
    total_token_score: float = 0.0
    total_structural_count: int = 0
    total_structural_score: float = 0.0
    total_leaf_count: int = 0
    total_leaf_score: float = 0.0
    failed_reason: Optional[str] = None
    evaluation_timestamp: Optional[datetime] = None
    merged_pull_requests: List[PullRequest] = field(default_factory=list)
    open_pull_requests: List[PullRequest] = field(default_factory=list)
    closed_pull_requests: List[PullRequest] = field(default_factory=list)
    unique_repos_contributed_to: Set[str] = field(default_factory=set)

    # Eligibility and credibility
    is_eligible: bool = False
    credibility: float = 0.0

    # Issue discovery scoring
    issue_discovery_score: float = 0.0
    issue_token_score: float = 0.0  # sum of solving PR token_scores for scored issues
    issue_credibility: float = 0.0
    is_issue_eligible: bool = False
    total_solved_issues: int = 0
    total_valid_solved_issues: int = 0  # solved issues where solving PR has token_score >= 5
    total_closed_issues: int = 0
    total_open_issues: int = 0

    @property
    def total_prs(self) -> int:
        return self.total_merged_prs + self.total_closed_prs + self.total_open_prs

    @property
    def total_merged_prs(self) -> int:
        return len(self.merged_pull_requests)

    @property
    def total_open_prs(self) -> int:
        return len(self.open_pull_requests)

    @property
    def total_closed_prs(self) -> int:
        return len(self.closed_pull_requests)

    def get_all_issues(self) -> List[Issue]:
        """Aggregate all issues from all pull requests (merged, open, closed)."""
        all_issues = []
        for pr in self.merged_pull_requests + self.open_pull_requests + self.closed_pull_requests:
            if pr.issues:
                all_issues.extend(pr.issues)
        return all_issues

    def get_all_file_changes(self) -> List[FileChange]:
        """Aggregate all file changes from all PR diffs (merged, open, closed)."""
        all_file_changes = []
        for pr in self.merged_pull_requests + self.open_pull_requests + self.closed_pull_requests:
            if pr.file_changes:
                all_file_changes.extend(pr.file_changes)
        return all_file_changes

    def add_merged_pull_request(self, raw_pr: Dict):
        """Add a merged pull request that will be factored into scoring."""
        bt.logging.info(
            f"Accepting MERGED PR #{raw_pr['number']} in {parse_repo_name(raw_pr['repository'])} -> '{raw_pr['baseRefName']}'"
        )
        self.merged_pull_requests.append(
            PullRequest.from_graphql_response(raw_pr, self.uid, self.hotkey, self.github_id)
        )

    def add_open_pull_request(self, raw_pr: Dict):
        """Add an open pull request that will be factored into scoring."""
        bt.logging.info(f'Counting OPEN PR #{raw_pr["number"]} in {parse_repo_name(raw_pr["repository"])}')
        self.open_pull_requests.append(PullRequest.from_graphql_response(raw_pr, self.uid, self.hotkey, self.github_id))

    def add_closed_pull_request(self, raw_pr: Dict):
        """Add a closed pull request that will be factored into scoring."""
        bt.logging.info(
            f'CLOSED PR #{raw_pr["number"]} in {parse_repo_name(raw_pr["repository"])} counting towards credibility'
        )
        self.closed_pull_requests.append(
            PullRequest.from_graphql_response(raw_pr, self.uid, self.hotkey, self.github_id)
        )


@dataclass
class ScoreBreakdown:
    """Breakdown of scores by type (structural vs leaf) and change type (added vs deleted).

    With tree-diff scoring, we track nodes that differ between old and new ASTs:
    - Added: nodes in new tree but not in old tree
    - Deleted: nodes in old tree but not in new tree

    Both additions and deletions represent meaningful work and are scored.
    """

    # Structural changes (function/class definitions, control flow, etc.)
    structural_added_count: int = 0
    structural_added_score: float = 0.0
    structural_deleted_count: int = 0
    structural_deleted_score: float = 0.0

    # Leaf token changes (identifiers, literals, operators, etc.)
    leaf_added_count: int = 0
    leaf_added_score: float = 0.0
    leaf_deleted_count: int = 0
    leaf_deleted_score: float = 0.0

    @property
    def total_score(self) -> float:
        """Total score for this file"""
        return (
            self.structural_added_score
            + self.structural_deleted_score
            + self.leaf_added_score
            + self.leaf_deleted_score
        )

    @property
    def structural_count(self) -> int:
        """Total structural changes (added + deleted)."""
        return self.structural_added_count + self.structural_deleted_count

    @property
    def structural_score(self) -> float:
        """Total structural score (added + deleted)."""
        return self.structural_added_score + self.structural_deleted_score

    @property
    def leaf_count(self) -> int:
        """Total leaf changes (added + deleted)."""
        return self.leaf_added_count + self.leaf_deleted_count

    @property
    def leaf_score(self) -> float:
        """Total leaf score (added + deleted)."""
        return self.leaf_added_score + self.leaf_deleted_score

    @property
    def added_count(self) -> int:
        """Total added nodes (structural + leaf)."""
        return self.structural_added_count + self.leaf_added_count

    @property
    def deleted_count(self) -> int:
        """Total deleted nodes (structural + leaf)."""
        return self.structural_deleted_count + self.leaf_deleted_count

    def with_weight(self, weight: float) -> 'ScoreBreakdown':
        """Return new ScoreBreakdown with scores multiplied by weight (counts unchanged)."""
        return ScoreBreakdown(
            structural_added_count=self.structural_added_count,
            structural_added_score=self.structural_added_score * weight,
            structural_deleted_count=self.structural_deleted_count,
            structural_deleted_score=self.structural_deleted_score * weight,
            leaf_added_count=self.leaf_added_count,
            leaf_added_score=self.leaf_added_score * weight,
            leaf_deleted_count=self.leaf_deleted_count,
            leaf_deleted_score=self.leaf_deleted_score * weight,
        )

    def __add__(self, other: 'ScoreBreakdown') -> 'ScoreBreakdown':
        """Sum two breakdowns together.

        Enables: sum(breakdowns, start=ScoreBreakdown())
        """
        return ScoreBreakdown(
            structural_added_count=self.structural_added_count + other.structural_added_count,
            structural_added_score=self.structural_added_score + other.structural_added_score,
            structural_deleted_count=self.structural_deleted_count + other.structural_deleted_count,
            structural_deleted_score=self.structural_deleted_score + other.structural_deleted_score,
            leaf_added_count=self.leaf_added_count + other.leaf_added_count,
            leaf_added_score=self.leaf_added_score + other.leaf_added_score,
            leaf_deleted_count=self.leaf_deleted_count + other.leaf_deleted_count,
            leaf_deleted_score=self.leaf_deleted_score + other.leaf_deleted_score,
        )


class ScoringCategory(Enum):
    """Category of a scored file"""

    SOURCE = 'source'  # Non-test code files scored via tree-diff
    TEST = 'test'  # Test files (any scoring method)
    NON_CODE = 'non_code'  # Everything else (line-count, skipped, binary, etc.)


@dataclass
class FileScoreResult:
    """Result of scoring a single file."""

    filename: str
    score: float
    nodes_scored: int  # Number of AST nodes scored (for tree-diff) or lines (for line-count)
    total_lines: int
    is_test_file: bool
    scoring_method: str  # 'tree-diff', 'line-count', 'skipped-*'
    breakdown: Optional[ScoreBreakdown] = None  # Only populated for tree-diff scoring

    @property
    def category(self) -> ScoringCategory:
        if self.is_test_file:
            return ScoringCategory.TEST
        if self.scoring_method == 'tree-diff':
            return ScoringCategory.SOURCE
        return ScoringCategory.NON_CODE


@dataclass
class PrScoringResult:
    """Result of scoring a pull request

    Contains aggregate metrics for the PR, including total score, per-file details,
    and optional per-category breakdowns.
    """

    total_score: float
    total_nodes_scored: int  # Total AST nodes scored across all files
    total_lines: int  # Total lines changed across all files
    file_results: List[FileScoreResult]
    score_breakdown: Optional[ScoreBreakdown] = None  # Aggregated breakdown across all files
    by_category: Dict[ScoringCategory, 'PrScoringResult'] = field(default_factory=dict)

    @property
    def density(self) -> float:
        """Code density (total_score / total_lines), capped at MAX_CODE_DENSITY_MULTIPLIER"""
        if self.total_lines <= 0:
            return 0.0
        return min(self.total_score / self.total_lines, MAX_CODE_DENSITY_MULTIPLIER)


@dataclass
class CachedEvaluation:
    hotkey: str
    github_id: str
    evaluation: 'MinerEvaluation'
    cached_at: datetime


class MinerEvaluationCache:
    """
    In-memory cache for successful miner evaluations, keyed by UID.

    Used as fallback when GitHub API is unavailable. Validates that
    hotkey and github_id match before returning cached data to handle
    miner re-registration on the same UID.
    """

    def __init__(self):
        self._cache: Dict[int, CachedEvaluation] = {}

    def store(self, evaluation: 'MinerEvaluation') -> None:
        """Store a successful evaluation in the cache."""
        if evaluation.failed_reason is not None:
            return

        if not evaluation.hotkey or not evaluation.github_id or evaluation.github_id == '0':
            return

        cached_eval = self.create_lightweight_copy(evaluation)

        self._cache[evaluation.uid] = CachedEvaluation(
            hotkey=evaluation.hotkey,
            github_id=evaluation.github_id,
            evaluation=cached_eval,
            cached_at=datetime.now(timezone.utc),
        )

        bt.logging.debug(f'Cached successful evaluation for UID {evaluation.uid}')

    def get(self, uid: int, hotkey: str, github_id: str) -> Optional['MinerEvaluation']:
        """
        Retrieve a cached evaluation if identity matches.

        Returns:
            Cached MinerEvaluation if found and identity matches, None otherwise
        """
        cached = self._cache.get(uid)

        if cached is None:
            return None

        if cached.hotkey != hotkey or cached.github_id != github_id:
            bt.logging.debug(
                f'Cache miss for UID {uid}: identity mismatch '
                f'(cached hotkey={cached.hotkey[:8]}..., github_id={cached.github_id} vs '
                f'current hotkey={hotkey[:8]}..., github_id={github_id}). '
                'Removing cached evaluation'
            )
            del self._cache[uid]
            return None

        bt.logging.debug(f'Cache hit for UID {uid} (cached at {cached.cached_at.isoformat()})')

        return deepcopy(cached.evaluation)

    def create_lightweight_copy(self, evaluation: 'MinerEvaluation') -> 'MinerEvaluation':
        """Create a memory-efficient copy, stripping file patches."""
        light_eval = deepcopy(evaluation)

        for pr in light_eval.merged_pull_requests + light_eval.open_pull_requests + light_eval.closed_pull_requests:
            if pr.file_changes:
                for fc in pr.file_changes:
                    fc.patch = None

        light_eval.github_pat = None

        return light_eval
