import copy
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import prod
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

import bittensor as bt

if TYPE_CHECKING:
    # Forward-reference only — avoids importing the mirror subpackage at runtime
    # and prevents accidental coupling. The mirror_* lists below are typed as
    # strings to defer resolution.
    from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR

from gittensor.constants import (
    EXTENSIONLESS_FILE_EXTENSIONS,
    MAX_CODE_DENSITY_MULTIPLIER,
)


def _apply_score_multipliers(base_score: float, multipliers: Dict[str, float], pr_label: str) -> float:
    """Compute earned score and emit the standard scoring log lines."""
    earned = base_score * prod(multipliers.values())
    mult_str = ' × '.join(f'{k}={v:.2f}' for k, v in multipliers.items())
    bt.logging.info(f'├─ {pr_label} → {earned:.2f}')
    bt.logging.info(f'│  └─ {base_score:.2f} × {mult_str}')
    return earned


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
        if '.' in basename:
            return basename.split('.')[-1].lower()
        basename_lower = basename.lower()
        return basename_lower if basename_lower in EXTENSIONLESS_FILE_EXTENSIONS else ''

    def is_test_file(self) -> bool:
        filename_lower = self.filename.lower()
        basename = filename_lower.split('/')[-1]

        test_dir_patterns = [
            r'(^|/)tests?/',
            r'(^|/)__tests?__/',
            r'(^|/)androidtest[a-z]*/',
            r'(^|/)integrationtest/',
            r'(^|/)spec/',
            r'\.tests?/',  # .NET MyProject.Tests/FooTests.cs
        ]
        if any(re.search(pattern, filename_lower) for pattern in test_dir_patterns):
            return True

        test_patterns = [
            r'^test_',
            r'^spec_',
            r'_test\.[^.]+$',
            r'_tests\.[^.]+$',
            r'_spec\.[^.]+$',
            r'\.test\.[^.]+$',
            r'\.tests\.[^.]+$',
            r'\.spec\.[^.]+$',
            r'^test\.[^.]+$',
            r'^tests\.[^.]+$',
            r'^conftest\.py$',
        ]

        return any(re.search(pattern, basename) for pattern in test_patterns)


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
    state_reason: Optional[str] = None  # "COMPLETED", "NOT_PLANNED", "TRANSFERRED", or None (legacy)
    updated_at: Optional[datetime] = None
    body_or_title_edited_at: Optional[datetime] = None
    discovery_base_score: float = 0.0
    discovery_earned_score: float = 0.0
    discovery_review_quality_multiplier: float = 1.0
    discovery_time_decay_multiplier: float = 1.0
    discovery_credibility_multiplier: float = 1.0
    discovery_open_issue_spam_multiplier: float = 1.0
    discovery_label_multiplier: float = 1.0

    @property
    def is_transferred(self) -> bool:
        """Convenience accessor. Prefer gating on `state_reason` directly in new code."""
        return self.state_reason == 'TRANSFERRED'


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
    base_score: float = 0.0
    issue_multiplier: float = 1.0
    open_pr_spam_multiplier: float = 1.0
    time_decay_multiplier: float = 1.0
    credibility_multiplier: float = 1.0
    review_quality_multiplier: float = 1.0  # Penalty for CHANGES_REQUESTED reviews from maintainers
    label_multiplier: float = 1.0  # Multiplier resolved from repository label config
    label: Optional[str] = None  # Resolved scoring label, set during scoring
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

    def calculate_final_earned_score(self) -> float:
        """Combine base score with all multipliers."""
        multipliers = {
            'issue': self.issue_multiplier,
            'label': self.label_multiplier,
            'spam': self.open_pr_spam_multiplier,
            'decay': self.time_decay_multiplier,
            'cred': self.credibility_multiplier,
            'review': self.review_quality_multiplier,
        }
        label = f'{self.pr_state.value} PR #{self.number} ({self.repository_full_name})'
        self.earned_score = _apply_score_multipliers(self.base_score, multipliers, label)
        return self.earned_score


@dataclass
class RepoEvaluation:
    """Per-repository scoring + eligibility result for one miner.

    Eligibility is computed per repo from only that repo's PRs/issues; one
    row per (miner, repo) is persisted to the miner_evaluations table.
    """

    repository_full_name: str

    # PR-side eligibility / scoring
    is_eligible: bool = False
    credibility: float = 0.0
    base_total_score: float = 0.0
    total_score: float = 0.0
    total_collateral_score: float = 0.0
    total_nodes_scored: int = 0
    total_token_score: float = 0.0
    total_structural_count: int = 0
    total_structural_score: float = 0.0
    total_leaf_count: int = 0
    total_leaf_score: float = 0.0
    total_merged_prs: int = 0
    total_open_prs: int = 0
    total_closed_prs: int = 0

    # Issue-discovery eligibility / scoring
    is_issue_eligible: bool = False
    issue_credibility: float = 0.0
    issue_discovery_score: float = 0.0
    issue_token_score: float = 0.0
    total_solved_issues: int = 0
    total_valid_solved_issues: int = 0
    total_closed_issues: int = 0
    total_open_issues: int = 0

    @property
    def total_prs(self) -> int:
        return self.total_merged_prs + self.total_open_prs + self.total_closed_prs

    def copy_issue_discovery_from(self, other: 'RepoEvaluation') -> None:
        """Copy the issue-discovery-owned fields from another RepoEvaluation."""
        for name in _ISSUE_DISCOVERY_REPO_FIELDS:
            setattr(self, name, getattr(other, name))


@dataclass
class MinerEvaluation:
    uid: int
    hotkey: str
    github_id: Optional[str] = '0'  # will be 0 if miner failed
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
    github_pr_fetch_failed: bool = False
    mirror_pr_fetch_failed: bool = False
    evaluation_timestamp: Optional[datetime] = None

    # Populated by gittensor.validator.oss_contributions.mirror.combine.combine.
    merged_prs: List['ScoredPR'] = field(default_factory=list)
    open_prs: List['ScoredPR'] = field(default_factory=list)
    closed_prs: List['ScoredPR'] = field(default_factory=list)

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
    total_open_issues: int = 0  # current mirror-tracked open issues (set by issue_discovery.scan)
    issue_discovery_issues: List[Issue] = field(default_factory=list)

    # Per-repository eligibility + scoring, keyed by lowercased repository_full_name.
    # The top-level scalars above are round-level rollups of this map.
    repo_evaluations: Dict[str, RepoEvaluation] = field(default_factory=dict)

    @property
    def total_prs(self) -> int:
        return self.total_merged_prs + self.total_closed_prs + self.total_open_prs

    @property
    def total_merged_prs(self) -> int:
        return len(self.merged_prs)

    @property
    def total_open_prs(self) -> int:
        return len(self.open_prs)

    @property
    def total_closed_prs(self) -> int:
        return len(self.closed_prs)

    @property
    def should_use_cache_fallback(self) -> bool:
        return self.github_pr_fetch_failed and self.total_prs == 0

    def get_all_issues(self) -> List[Issue]:
        """Aggregate all linked issues from mirror PRs, adapted to the ``Issue`` shape used by storage."""
        # Lazy import — mirror.adapters imports from classes.py (for Issue /
        # FileChange), so importing it at module load would loop back here.
        from gittensor.validator.oss_contributions.mirror.adapters import (
            mirror_linked_issue_to_legacy_issue,
        )

        all_issues = []
        for scored in self.merged_prs + self.open_prs + self.closed_prs:
            for li in scored.pr.linked_issues:
                all_issues.append(
                    mirror_linked_issue_to_legacy_issue(li, scored.pr.pr_number, scored.pr.repo_full_name)
                )
        return all_issues

    def get_all_file_changes(self) -> List[FileChange]:
        """Aggregate all file changes from mirror PR diffs."""
        from gittensor.validator.oss_contributions.mirror.adapters import (
            mirror_files_to_legacy,
        )

        all_file_changes = []
        for scored in self.merged_prs + self.open_prs + self.closed_prs:
            if scored.files:
                file_changes, _ = mirror_files_to_legacy(scored.pr.repo_full_name, scored.pr.pr_number, scored.files)
                all_file_changes.extend(file_changes)
        return all_file_changes


@dataclass
class RepoEmissionAllocation:
    """Per-repository allocation details for one scoring round."""

    repository_full_name: str
    emission_share: float
    issue_discovery_share: float
    repo_slice: float
    maintainer_cut: float = 0.0
    maintainer_carve_out: float = 0.0
    maintainer_rewards: Dict[int, float] = field(default_factory=dict)
    pr_slice: float = 0.0
    issue_discovery_slice: float = 0.0
    pr_scores: Dict[int, float] = field(default_factory=dict)
    issue_discovery_scores: Dict[int, float] = field(default_factory=dict)
    pr_rewards: Dict[int, float] = field(default_factory=dict)
    issue_discovery_rewards: Dict[int, float] = field(default_factory=dict)
    recycled_amount: float = 0.0


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


# Fields owned by the issue-discovery phase. store() preserves these across
# rounds so the OSS-phase write doesn't clobber the prior round's refresh;
# update_issue_discovery() is the authoritative writer.
_ISSUE_DISCOVERY_FIELDS: Tuple[str, ...] = (
    'issue_discovery_score',
    'issue_token_score',
    'issue_credibility',
    'is_issue_eligible',
    'total_solved_issues',
    'total_valid_solved_issues',
    'total_closed_issues',
    'total_open_issues',
    'issue_discovery_issues',
)

# Per-repo RepoEvaluation fields owned by the issue-discovery phase — preserved
# across rounds by store() exactly like the top-level _ISSUE_DISCOVERY_FIELDS.
_ISSUE_DISCOVERY_REPO_FIELDS: Tuple[str, ...] = (
    'is_issue_eligible',
    'issue_credibility',
    'issue_discovery_score',
    'issue_token_score',
    'total_solved_issues',
    'total_valid_solved_issues',
    'total_closed_issues',
    'total_open_issues',
)


class MinerEvaluationCache:
    """
    In-memory cache for successful miner evaluations, keyed by UID.

    Used as fallback when GitHub API is unavailable. Validates that
    hotkey and github_id match before returning cached data to handle
    miner re-registration on the same UID.

    The cache has two independent writers: store() is called by the OSS
    phase with a freshly-fetched MinerEvaluation whose issue-discovery
    fields are dataclass defaults, and update_issue_discovery() is called
    by the issue-discovery phase after scoring. store() therefore preserves
    any prior round's issue-discovery fields when identity matches, so a
    later same-round mirror outage can fall back to a non-zero score.
    """

    def __init__(self):
        self._cache: Dict[int, CachedEvaluation] = {}

    def store(self, evaluation: 'MinerEvaluation') -> None:
        """Store a successful evaluation in the cache.

        Preserves the prior entry's issue-discovery fields when the UID's
        identity (hotkey, github_id) is unchanged — the caller (OSS phase)
        is not authoritative for those fields and writes dataclass defaults
        every round. Identity mismatch (re-registration) drops the prior
        issue-discovery state along with the rest of the entry.
        """
        if evaluation.failed_reason is not None:
            return

        if not evaluation.hotkey or not evaluation.github_id or evaluation.github_id == '0':
            return

        cached_eval = self._build_cache_entry(evaluation)

        existing = self._cache.get(evaluation.uid)
        if existing is not None and existing.hotkey == evaluation.hotkey and existing.github_id == evaluation.github_id:
            for name in _ISSUE_DISCOVERY_FIELDS:
                value = getattr(existing.evaluation, name)
                setattr(cached_eval, name, _copy_issue_discovery_value(name, value))
            for repo_name, prior_repo in existing.evaluation.repo_evaluations.items():
                target = cached_eval.repo_evaluations.get(repo_name)
                if target is None:
                    target = RepoEvaluation(repository_full_name=prior_repo.repository_full_name)
                    cached_eval.repo_evaluations[repo_name] = target
                target.copy_issue_discovery_from(prior_repo)

        self._cache[evaluation.uid] = CachedEvaluation(
            hotkey=evaluation.hotkey,
            github_id=evaluation.github_id,
            evaluation=cached_eval,
            cached_at=datetime.now(timezone.utc),
        )

        bt.logging.debug(f'Cached successful evaluation for UID {evaluation.uid}')

    def update_issue_discovery(self, evaluation: 'MinerEvaluation') -> None:
        """Refresh issue-discovery fields on an existing cache entry.

        No-op when no entry exists for this UID. Missing entries occur on
        identity-mismatch evictions or OSS-phase failures; in both cases we
        let the next round's store() re-anchor the entry rather than write
        a half-populated one here. The OSS fallback path additionally
        guards against restoring an entry with no PR data.
        """
        existing = self._cache.get(evaluation.uid)
        if existing is None:
            return

        if existing.hotkey != evaluation.hotkey or existing.github_id != evaluation.github_id:
            bt.logging.debug(
                f'Skipping issue-discovery refresh for UID {evaluation.uid}: identity mismatch '
                f'(cached hotkey={existing.hotkey[:8]}..., github_id={existing.github_id} vs '
                f'current hotkey={evaluation.hotkey[:8]}..., github_id={evaluation.github_id}). '
                'Removing cached evaluation'
            )
            del self._cache[evaluation.uid]
            return

        for name in _ISSUE_DISCOVERY_FIELDS:
            value = getattr(evaluation, name)
            setattr(existing.evaluation, name, _copy_issue_discovery_value(name, value))

        for repo_name, repo_eval in evaluation.repo_evaluations.items():
            target = existing.evaluation.repo_evaluations.get(repo_name)
            if target is None:
                target = RepoEvaluation(repository_full_name=repo_eval.repository_full_name)
                existing.evaluation.repo_evaluations[repo_name] = target
            target.copy_issue_discovery_from(repo_eval)

        bt.logging.debug(f'Refreshed cached issue discovery for UID {evaluation.uid}')

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

        return self._isolate_for_downstream(cached.evaluation)

    def evict_many(self, uids: Set[int]) -> None:
        """Remove cached evaluations for all provided UIDs."""
        for uid in uids:
            if self._cache.pop(uid, None) is not None:
                bt.logging.debug(f'Evicted cached evaluation for UID {uid}')

    @staticmethod
    def _build_cache_entry(evaluation: 'MinerEvaluation') -> 'MinerEvaluation':
        # Cached evaluations feed only the GitHub-fetch-failure fallback path
        # (issue_competitions + issue discovery scoring), which never reads
        # PR files. Drop them at store time to save memory.
        cached = copy.copy(evaluation)
        cached.unique_repos_contributed_to = set(evaluation.unique_repos_contributed_to)
        cached.issue_discovery_issues = _copy_issue_discovery_issues(evaluation.issue_discovery_issues)
        cached.repo_evaluations = {name: copy.copy(re) for name, re in evaluation.repo_evaluations.items()}
        cached.merged_prs = [_scored_mirror_pr_for_cache(pr) for pr in evaluation.merged_prs]
        cached.open_prs = [_scored_mirror_pr_for_cache(pr) for pr in evaluation.open_prs]
        cached.closed_prs = [_scored_mirror_pr_for_cache(pr) for pr in evaluation.closed_prs]
        return cached

    @staticmethod
    def _isolate_for_downstream(cached_eval: 'MinerEvaluation') -> 'MinerEvaluation':
        # Downstream scoring mutates top-level scalar fields on MinerEvaluation
        # and discovery_* fields on Issue. Mirror PRs are shared — the issue
        # adapters produce fresh Issue objects per call via get_all_issues().
        copy_eval = copy.copy(cached_eval)
        copy_eval.unique_repos_contributed_to = set(cached_eval.unique_repos_contributed_to)
        copy_eval.issue_discovery_issues = _copy_issue_discovery_issues(cached_eval.issue_discovery_issues)
        copy_eval.repo_evaluations = {name: copy.copy(re) for name, re in cached_eval.repo_evaluations.items()}
        return copy_eval


def _copy_issue_discovery_value(name: str, value):
    return _copy_issue_discovery_issues(value) if name == 'issue_discovery_issues' else value


def _copy_issue_discovery_issues(issues: List[Issue]) -> List[Issue]:
    return [copy.copy(issue) for issue in issues]


def _scored_mirror_pr_for_cache(scored: 'ScoredPR') -> 'ScoredPR':
    scored_copy = copy.copy(scored)
    scored_copy.files = None
    return scored_copy
