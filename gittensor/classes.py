import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import prod
from typing import DefaultDict, Dict, List, Optional, Set

import bittensor as bt

from gittensor.utils.utils import parse_repo_name
from gittensor.validator.configurations.tier_config import Tier, TierConfig, TierStats

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
class Repository:
    """Repository information"""

    name: str
    owner: str
    weight: float

    @property
    def full_name(self) -> str:
        return f'{self.owner}/{self.name}'


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
        return self.filename.split('.')[-1].lower() if '.' in self.filename else ''

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


@dataclass
class PullRequest:
    """Represents a pull request with relevant metadata for scoring.

    Supports both MERGED PRs (earned scores) and OPEN PRs (collateral scores).
    """

    number: int
    repository_full_name: str
    uid: int
    hotkey: str
    github_id: str
    title: str
    author_login: str
    merged_at: Optional[datetime]  # None for OPEN PRs
    created_at: datetime

    # PR state based fields
    pr_state: PRState
    repository_tier_configuration: Optional[TierConfig] = None  # assigned when scoring PR

    # Score fields
    repo_weight_multiplier: float = 1.0
    base_score: float = 0.0
    issue_multiplier: float = 1.0
    open_pr_spam_multiplier: float = 1.0
    pioneer_multiplier: float = 1.0
    pioneer_rank: int = 0
    time_decay_multiplier: float = 1.0
    credibility_multiplier: float = 1.0
    raw_credibility: float = 1.0  # Before applying ^k scalar
    credibility_scalar: int = 1  # The k value from tier config
    earned_score: float = 0.0
    collateral_score: float = 0.0  # For OPEN PRs: potential_score * collateral_percent

    # Contribution details
    additions: int = 0
    deletions: int = 0
    commits: int = 0
    total_nodes_scored: int = 0  # Total AST nodes scored for this PR

    # Token scoring breakdown (after test weight applied)
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

    def calculate_final_earned_score(self) -> float:
        """Combine base score with all multipliers."""
        multipliers = {
            'repo': self.repo_weight_multiplier,
            'issue': self.issue_multiplier,
            'spam': self.open_pr_spam_multiplier,
            'pioneer': self.pioneer_multiplier,
            'decay': self.time_decay_multiplier,
            'cred': self.credibility_multiplier,
        }

        self.earned_score = self.base_score * prod(multipliers.values())

        # Log all multipliers (credibility shows ^k format)
        mult_str = ' × '.join(
            f'cred={self.raw_credibility:.2f}^{self.credibility_scalar}'
            if k == 'cred'
            else f'pioneer={v:.2f}({"P" if self.pioneer_rank == 1 else "F"})'
            if k == 'pioneer'
            else f'{k}={v:.2f}'
            for k, v in multipliers.items()
        )
        bt.logging.info(
            f'├─ {self.pr_state.value} PR #{self.number} ({self.repository_full_name}) → {self.earned_score:.2f}'
        )
        bt.logging.info(f'│  └─ {self.base_score:.2f} × {mult_str}')

        return self.earned_score

    @classmethod
    def from_graphql_response(cls, pr_data: dict, uid: int, hotkey: str, github_id: str) -> 'PullRequest':
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
            issues.append(
                Issue(
                    number=issue['number'],
                    pr_number=pr_data['number'],
                    repository_full_name=repository_full_name,
                    title=issue['title'],
                    created_at=parse_github_timestamp_to_cst(issue['createdAt']) if issue.get('createdAt') else None,
                    closed_at=parse_github_timestamp_to_cst(issue['closedAt']) if issue.get('closedAt') else None,
                    author_login=issue.get('author', {}).get('login') if issue.get('author') else None,
                    state=issue.get('state'),
                    author_association=issue.get('authorAssociation'),
                )
            )

        description: str = pr_data.get('bodyText', '')
        last_edited_at = (
            parse_github_timestamp_to_cst(pr_data.get('lastEditedAt')) if pr_data.get('lastEditedAt') else None
        )
        merged_at = parse_github_timestamp_to_cst(pr_data['mergedAt']) if is_merged else None

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
    qualified_unique_repos_count: int = 0  # Repos meeting min token score threshold

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

    # Tier level details (None = no tier unlocked yet)
    current_tier: Optional[Tier] = None
    credibility_by_tier: Dict[Tier, float] = field(default_factory=dict)
    stats_by_tier: Dict[Tier, TierStats] = field(default_factory=lambda: {tier: TierStats() for tier in Tier})

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
    def added_score(self) -> float:
        """Total score from additions."""
        return self.structural_added_score + self.leaf_added_score

    @property
    def deleted_count(self) -> int:
        """Total deleted nodes (structural + leaf)."""
        return self.structural_deleted_count + self.leaf_deleted_count

    @property
    def deleted_score(self) -> float:
        """Total score from deletions."""
        return self.structural_deleted_score + self.leaf_deleted_score

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


@dataclass
class PrScoringResult:
    """Result of scoring a pull request.

    Contains aggregate metrics for the PR, including total score and per-file details.
    """

    total_score: float
    total_nodes_scored: int  # Total AST nodes scored across all files
    file_results: List[FileScoreResult]
    score_breakdown: Optional[ScoreBreakdown] = None  # Aggregated breakdown across all files


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

    def invalidate(self, uid: int) -> None:
        """Remove a cached evaluation for a specific UID."""
        if uid in self._cache:
            del self._cache[uid]
            bt.logging.debug(f'Invalidated cache for UID {uid}')

    def clear(self) -> None:
        """Clear all cached evaluations."""
        self._cache.clear()
        bt.logging.info('Cleared evaluation cache')

    def create_lightweight_copy(self, evaluation: 'MinerEvaluation') -> 'MinerEvaluation':
        """Create a memory-efficient copy, stripping file patches."""
        light_eval = deepcopy(evaluation)

        for pr in light_eval.merged_pull_requests + light_eval.open_pull_requests + light_eval.closed_pull_requests:
            if pr.file_changes:
                for fc in pr.file_changes:
                    fc.patch = None

        light_eval.github_pat = None

        return light_eval
