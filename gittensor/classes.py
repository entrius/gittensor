import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import DefaultDict, Dict, List, Optional, Set
from math import prod

import bittensor as bt

from gittensor.constants import (
    DEFAULT_PROGRAMMING_LANGUAGE_WEIGHT,
    MAX_LINES_SCORED_FOR_MITIGATED_EXT,
    MITIGATED_EXTENSIONS,
    TEST_FILE_CONTRIBUTION_WEIGHT,
)

GITHUB_DOMAIN = 'https://github.com/'


class PRState(Enum):
    """PR state for scoring: MERGED (earned) or OPEN (collateral)."""
    MERGED = "MERGED"
    OPEN = "OPEN"


@dataclass
class PRFetchResult:
    """Result from fetching user PRs via GraphQL API."""

    merged_prs: List[DefaultDict]  # PRs with state MERGED (eligible for scoring)
    open_prs: List[DefaultDict]  # PRs with state OPEN (for collateral scoring)
    open_pr_count: int  # Total open PRs in active repos
    merged_pr_count: int  # Merged PRs after MERGE_SUCCESS_RATIO_APPLICATION_DATE
    closed_pr_count: int  # Closed (not merged) PRs after MERGE_SUCCESS_RATIO_APPLICATION_DATE

@dataclass
class Miner:
    """Miner identity"""

    uid: int
    hotkey: str
    github_id: str

    def __str__(self) -> str:
        return f"Miner(uid={self.uid}, hotkey={self.hotkey[:8]}..., github_id={self.github_id})"


@dataclass
class Repository:
    """Repository information"""

    name: str
    owner: str
    weight: float

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class FileChange:
    """Represents a single file change in a PR"""

    pr_number: int
    repository_full_name: str
    filename: str
    changes: int
    additions: int
    deletions: int
    status: str  # "added", "modified", "removed", etc.
    patch: Optional[str] = None  # The actual diff content
    file_extension: Optional[str] = None

    @property
    def short_name(self) -> str:
        """Return only the base filename (strip directories)."""
        return self.filename.split("/")[-1]

    def __post_init__(self):
        if self.file_extension is None:
            self.file_extension = self._calculate_file_extension()

    def _calculate_file_extension(self) -> str:
        return self.filename.split(".")[-1].lower() if "." in self.filename else ""

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

    # PR state for collateral system
    pr_state: PRState = PRState.MERGED

    # Score fields
    repo_weight_multiplier: float = 1.0
    base_score: float = 0.0
    issue_multiplier: float = 1.0
    open_pr_spam_multiplier: float = 1.0
    repository_uniqueness_multiplier: float = 1.0
    time_decay_multiplier: float = 1.0
    gittensor_tag_multiplier: float = 1.0
    merge_success_multiplier: float = 1.0
    earned_score: float = 0.0
    collateral_score: float = 0.0  # For OPEN PRs: potential_score * collateral_percent

    # Contribution details
    additions: int = 0
    deletions: int = 0
    commits: int = 0
    total_lines_scored: int = 0    
    gittensor_tagged: bool = False
    merged_by_login: Optional[str] = None
    file_changes: Optional[List[FileChange]] = None
    issues: Optional[List[Issue]] = None
    description: Optional[str] = None
    last_edited_at: Optional[datetime] = None

    def set_file_changes(self, file_changes: List[FileChange]) -> None:
        """Set the file changes for this pull request"""
        self.file_changes = file_changes

    def calculate_score_from_file_changes(self, programming_languages: Dict[str, float]) -> float:
        """Calculate the score for a single PR based on its file changes."""
        # Import here to avoid circular import
        from gittensor.validator.utils.spam_detection import count_non_scoreable_lines

        if not self.file_changes:
            return 0.0

        pr_score = 0.0

        total_files_changed = len(self.file_changes)
        bt.logging.info(f"\nScoring {total_files_changed} file changes for PR #{self.number}")

        for n, file in enumerate(self.file_changes, start=1):
            language_weight = programming_languages.get(file.file_extension, DEFAULT_PROGRAMMING_LANGUAGE_WEIGHT)

            total_changes_to_score = file.changes
            if file.file_extension in MITIGATED_EXTENSIONS:
                total_changes_to_score = min(file.changes, MAX_LINES_SCORED_FOR_MITIGATED_EXT)

            non_scoreable_lines = count_non_scoreable_lines(file.patch, total_changes_to_score, file.file_extension)
            scored_changes = max(0, total_changes_to_score - non_scoreable_lines)

            self.total_lines_scored += scored_changes
            file_weight = TEST_FILE_CONTRIBUTION_WEIGHT if file.is_test_file() else 1.0

            file_score = language_weight * file_weight * scored_changes

            bt.logging.info(f"   -  [{n}/{total_files_changed}] - {file.short_name} | scored {scored_changes} / {file.changes} lines | score: {file_score:.2f}")
            pr_score += file_score

        bt.logging.info(f"Base PR score from file changes: {pr_score:.2f}")
        return pr_score
    
    def calculate_final_earned_score(self) -> float:
        """Combine base score with all multipliers."""
        multipliers = {
            "repo_weight_multiplier": self.repo_weight_multiplier,
            "issue_multiplier": self.issue_multiplier,
            "open_pr_spam_multiplier": self.open_pr_spam_multiplier,
            "repo_uniqueness_multiplier": self.repository_uniqueness_multiplier,
            "time_decay_multiplier": self.time_decay_multiplier,
            "gittensor_tag_multiplier": self.gittensor_tag_multiplier,
            "merge_success_multiplier": self.merge_success_multiplier,
        }

        self.earned_score = self.base_score * prod(multipliers.values())
        mult_str = " | ".join([f"{k}: {v:.2f}" for k, v in multipliers.items()])

        bt.logging.info(
            f"PR #{self.number} -> {self.repository_full_name} | base: {self.base_score:.2f} | {mult_str} | final: {self.earned_score:.2f}"
        )

        return self.earned_score

    @classmethod
    def from_graphql_response(cls, pr_data: dict, uid: int, hotkey: str, github_id: str) -> 'PullRequest':
        """Create PullRequest from GraphQL API response"""
        # Import here to avoid circular dependency
        from gittensor.constants import PR_TAGLINE
        from gittensor.validator.utils.datetime_utils import parse_github_timestamp

        repo_data = pr_data['repository']
        repository_full_name = f"{repo_data['owner']['login']}/{repo_data['name']}"

        raw_issues = pr_data['closingIssuesReferences']['nodes']
        issues = []
        for issue in raw_issues:
            # Only include issues that are actually closed (both closedAt timestamp and CLOSED state)
            if issue['closedAt'] and issue.get('state') == 'CLOSED':
                issues.append(
                    Issue(
                        number=issue['number'],
                        pr_number=pr_data['number'],
                        repository_full_name=repository_full_name,
                        title=issue['title'],
                        created_at=parse_github_timestamp(issue['createdAt']),
                        closed_at=parse_github_timestamp(issue['closedAt']),
                        author_login=issue.get('author', {}).get('login') if issue.get('author') else None,
                        state=issue.get('state'),
                    )
                )

        # Extract description and check for Gittensor tagline
        description = pr_data.get('bodyText', '')
        last_edited_at = parse_github_timestamp(pr_data.get('lastEditedAt')) if pr_data.get('lastEditedAt') else None
        merged_at = parse_github_timestamp(pr_data['mergedAt'])

        # Check if PR has Gittensor tagline and wasn't edited after merge
        gittensor_tagged = False
        if description:
            # Get the last 100 characters (with cushion) and trim whitespace
            description_end = description[-100:].strip()
            # Check if it ends with the tagline (case-insensitive, lenient with trailing punctuation)
            description_end_cleaned = description_end.rstrip('.,!?;: \t\n')
            if description_end_cleaned.lower().endswith(PR_TAGLINE.lower()):
                # Only set tagged to True if PR was NOT edited after being merged
                # (to prevent miners from editing after merge to add the tagline)
                if last_edited_at is None or last_edited_at <= merged_at:
                    gittensor_tagged = True
                else:
                    bt.logging.warning(
                        f"PR #{str(pr_data['number'])} in {repository_full_name} has Gittensor tagline but was edited after merge "
                        f"(merged: {merged_at.isoformat()}, last edited: {last_edited_at.isoformat()})"
                    )

        return cls(
            number=pr_data['number'],
            repository_full_name=repository_full_name,
            uid=uid,
            hotkey=hotkey,
            github_id=github_id,
            title=pr_data['title'],
            author_login=pr_data['author']['login'],
            merged_at=merged_at,
            created_at=parse_github_timestamp(pr_data['createdAt']),
            pr_state=PRState.MERGED,
            additions=pr_data['additions'],
            deletions=pr_data['deletions'],
            commits=pr_data.get('commits', {}).get('totalCount', 0),
            merged_by_login=pr_data['mergedBy']['login'] if pr_data.get('mergedBy') else None,
            issues=issues,
            description=description,
            last_edited_at=last_edited_at,
            gittensor_tagged=gittensor_tagged,
        )

    @classmethod
    def from_open_pr_graphql_response(cls, pr_data: dict, uid: int, hotkey: str, github_id: str) -> 'PullRequest':
        """Create PullRequest from GraphQL API response for an OPEN PR (collateral system).

        Extracts linked issues for collateral issue_multiplier calculation.
        """
        from gittensor.constants import PR_TAGLINE
        from gittensor.validator.utils.datetime_utils import parse_github_timestamp

        repo_data = pr_data['repository']
        repository_full_name = f"{repo_data['owner']['login']}/{repo_data['name']}"

        # Extract linked issues for collateral calculation (may be open or closed)
        raw_issues = pr_data.get('closingIssuesReferences', {}).get('nodes', [])
        issues = []
        for issue in raw_issues:
            issues.append(
                Issue(
                    number=issue['number'],
                    pr_number=pr_data['number'],
                    repository_full_name=repository_full_name,
                    title=issue['title'],
                    created_at=parse_github_timestamp(issue['createdAt']) if issue.get('createdAt') else None,
                    closed_at=parse_github_timestamp(issue['closedAt']) if issue.get('closedAt') else None,
                    author_login=issue.get('author', {}).get('login') if issue.get('author') else None,
                    state=issue.get('state'),
                )
            )

        description = pr_data.get('bodyText', '')
        last_edited_at = parse_github_timestamp(pr_data.get('lastEditedAt')) if pr_data.get('lastEditedAt') else None

        # Detect Gittensor tagline for collateral calculation
        gittensor_tagged = False
        if description:
            description_end = description[-100:].strip().rstrip('.,!?;: \t\n')
            gittensor_tagged = description_end.lower().endswith(PR_TAGLINE.lower())

        return cls(
            number=pr_data['number'],
            repository_full_name=repository_full_name,
            uid=uid,
            hotkey=hotkey,
            github_id=github_id,
            title=pr_data['title'],
            author_login=pr_data['author']['login'],
            merged_at=None,
            created_at=parse_github_timestamp(pr_data['createdAt']),
            pr_state=PRState.OPEN,
            additions=pr_data['additions'],
            deletions=pr_data['deletions'],
            commits=pr_data.get('commits', {}).get('totalCount', 0),
            merged_by_login=None,
            issues=issues if issues else None,
            description=description,
            last_edited_at=last_edited_at,
            gittensor_tagged=gittensor_tagged,
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
    total_lines_changed: int = 0
    total_open_prs: int = 0
    total_closed_prs: int = 0  # Total PRs closed within MERGED_PR_LOOKBACK_DAYS
    total_merged_prs: int = 0  # Total PRs merged within MERGED_PR_LOOKBACK_DAYS (len of valid_prs)
    unique_repos_count: int = 0
    failed_reason: Optional[str] = None
    evaluation_timestamp: Optional[datetime] = None
    merged_pull_requests: List[PullRequest] = field(default_factory=list)
    open_pull_requests: List[PullRequest] = field(default_factory=list)
    unique_repos_contributed_to: Set[str] = field(default_factory=set)

    @property
    def total_prs(self) -> int:
        return len(self.merged_pull_requests)

    @property
    def pull_requests(self) -> List[PullRequest]:
        """Backwards compatibility alias for merged_pull_requests."""
        return self.merged_pull_requests

    def set_invalid_response_reason(self, reason: str):
        """
        Sets the reason for why a miners evaluation may have failed.

        Args:
            reason: The failure reason
        """
        self.failed_reason = reason

    def get_all_issues(self) -> List[Issue]:
        """Aggregate all issues from all merged pull requests."""
        all_issues = []
        for pr in self.merged_pull_requests:
            if pr.issues:
                all_issues.extend(pr.issues)
        return all_issues

    def get_all_file_changes(self) -> List[FileChange]:
        """Aggregate all file changes from all merged PR diffs."""
        all_file_changes = []
        for pr in self.merged_pull_requests:
            if pr.file_changes:
                all_file_changes.extend(pr.file_changes)
        return all_file_changes

    def add_merged_pull_request(self, pull_request: PullRequest):
        """Add a merged pull request."""
        self.merged_pull_requests.append(pull_request)

    def add_open_pull_request(self, pull_request: PullRequest):
        """Add an open pull request for collateral scoring."""
        self.open_pull_requests.append(pull_request)


class GitPatSynapse(bt.Synapse):
    """
    This synapse is used to request GitHub access tokens from a miner and receive the response.

    Attributes:
    - github_access_token: A string value representing the GitHub access token.
      Initially None for requests, and set to the actual token for responses.
    """

    github_access_token: Optional[str] = None
