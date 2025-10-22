from dataclasses import dataclass, field
from datetime import datetime
from typing import DefaultDict, List, Optional, Set

import bittensor as bt

from gittensor.constants import EXCESSIVE_PR_MIN_WEIGHT, EXCESSIVE_PR_PENALTY_SLOPE, EXCESSIVE_PR_PENALTY_THRESHOLD

GITHUB_DOMAIN = 'https://github.com/'


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

    def __post_init__(self):
        if self.file_extension is None:
            self.file_extension = self._calculate_file_extension()

    def _calculate_file_extension(self) -> str:
        return self.filename.split(".")[-1].lower() if "." in self.filename else ""

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


@dataclass
class PullRequest:
    """Represents a merged pull request with relevant metadata"""

    number: int
    repository_full_name: str
    uid: int
    hotkey: str
    github_id: str
    title: str
    author_login: str
    merged_at: datetime
    created_at: datetime
    earned_score: float = 0.0
    additions: int = 0
    deletions: int = 0
    commits: int = 0
    merged_by_login: Optional[str] = None
    file_changes: Optional[List[FileChange]] = None
    issues: Optional[List[Issue]] = None

    @property
    def total_changes(self) -> int:
        """Total lines changed (additions + deletions)"""
        return self.additions + self.deletions

    def set_earned_score(self, score: float) -> None:
        """Set the earned score for this pull request"""
        self.earned_score = score

    def set_file_changes(self, file_changes: List[FileChange]) -> None:
        """Set the file changes for this pull request"""
        self.file_changes = file_changes

    @classmethod
    def from_graphql_response(cls, pr_data: dict, uid: int, hotkey: str, github_id: str) -> 'PullRequest':
        """Create PullRequest from GraphQL API response"""
        # Import here to avoid circular dependency
        from gittensor.validator.utils.datetime_utils import parse_github_timestamp

        repo_data = pr_data['repository']
        repository_full_name = f"{repo_data['owner']['login']}/{repo_data['name']}"

        raw_issues = pr_data['closingIssuesReferences']['nodes']
        issues = []
        for issue in raw_issues:
            if issue['closedAt']:
                issues.append(
                    Issue(
                        number=issue['number'],
                        pr_number=pr_data['number'],
                        repository_full_name=repository_full_name,
                        title=issue['title'],
                        created_at=parse_github_timestamp(issue['createdAt']),
                        closed_at=parse_github_timestamp(issue['closedAt']),
                    )
                )

        return cls(
            number=pr_data['number'],
            repository_full_name=repository_full_name,
            uid=uid,
            hotkey=hotkey,
            github_id=github_id,
            title=pr_data['title'],
            author_login=pr_data['author']['login'],
            merged_at=parse_github_timestamp(pr_data['mergedAt']),
            created_at=parse_github_timestamp(pr_data['createdAt']),
            additions=pr_data['additions'],
            deletions=pr_data['deletions'],
            commits=pr_data.get('commits', {}).get('totalCount', 0),
            merged_by_login=pr_data['mergedBy']['login'] if pr_data.get('mergedBy') else None,
            issues=issues,
        )


@dataclass
class MinerEvaluation:
    uid: int
    hotkey: str
    github_id: Optional[str] = '0'  # will be 0 if miner failed
    github_pat: Optional[str] = None
    total_score: float = 0.0
    total_lines_changed: int = 0
    total_open_prs: int = 0
    unique_repos_count: int = 0
    failed_reason: Optional[str] = None
    evaluation_timestamp: Optional[datetime] = None
    pull_requests: List[PullRequest] = field(default_factory=list)
    unique_repos_contributed_to: Set[str] = field(default_factory=set)

    @property
    def total_prs(self) -> int:
        """Total number of valid PRs - uses stored DB value if available, otherwise computes from pull_requests"""
        return len(self.pull_requests)

    def calculate_total_score_and_total_contributions(self):
        """Calculate total lines changed and unique repositories from PRs"""
        if not self.pull_requests:
            return

        self.total_score = sum(pr.earned_score for pr in self.pull_requests)
        self.apply_open_pr_spam_penalty_to_score()

        self.total_lines_changed = sum(pr.total_changes for pr in self.pull_requests)
        self.unique_repos_contributed_to = set(pr.repository_full_name for pr in self.pull_requests)
        self.unique_repos_count = len(self.unique_repos_contributed_to)

        bt.logging.info(f"Final evaluation for UID {self.uid}:")
        bt.logging.info(f"  - Total Score: {self.total_score:.5f}")
        bt.logging.info(f"  - Total Valid PRs: {self.total_prs}")
        bt.logging.info(f"  - Total open PRs: {self.total_open_prs}")
        bt.logging.info(f"  - Total Lines Changed: {self.total_lines_changed}")
        bt.logging.info(f"  - Unique Repositories Contibuted To: {self.get_unique_repositories()}")

    def apply_open_pr_spam_penalty_to_score(self):
        """
        Apply penalty for excessive open PRs with configurable parameters.

        Args:
            threshold: Number of open PRs before penalty kicks in
            min_weight: Minimum weight (maximum penalty)
            penalty_slope: How steep the penalty curve is
        """
        if self.total_open_prs > EXCESSIVE_PR_PENALTY_THRESHOLD:
            excess_pr_count = self.total_open_prs - EXCESSIVE_PR_PENALTY_THRESHOLD
            weight = max(EXCESSIVE_PR_MIN_WEIGHT, 1.0 - excess_pr_count * EXCESSIVE_PR_PENALTY_SLOPE)
            self.total_score = weight * self.total_score

    def set_invalid_response_reason(self, reason: str):
        """
        Sets the reason for why a miners evaluation may have failed.

        Args:
            reason: The failure reason
        """
        self.failed_reason = reason

    def get_all_issues(self) -> List[Issue]:
        """
        Aggregate all issues from all pull requests.
        Respects the natural object hierarchy: PullRequest -> Issues
        """
        all_issues = []
        for pr in self.pull_requests:
            if pr.issues:
                all_issues.extend(pr.issues)
        return all_issues

    def get_unique_repositories(self) -> Set[str]:
        """
        Get unique repository full names from all pull requests.
        Returns set of strings (e.g., "owner/repo") as expected by scoring functions.
        """
        repositories = set()

        # From pull requests (primary source)
        for pr in self.pull_requests:
            repositories.add(pr.repository_full_name)

        return repositories

    def get_all_file_changes(self) -> List[FileChange]:
        """
        Aggregate all file changes from all PR diffs.
        """
        all_file_changes = []
        for pr in self.pull_requests:
            if pr.file_changes:
                all_file_changes.extend(pr.file_changes)
        return all_file_changes

    def add_pull_request(self, pull_request: PullRequest):
        """Helper method to add a pull request and maintain collections."""
        self.pull_requests.append(pull_request)


class GitPatSynapse(bt.Synapse):
    """
    This synapse is used to request GitHub access tokens from a miner and receive the response.

    Attributes:
    - github_access_token: A string value representing the GitHub access token.
      Initially None for requests, and set to the actual token for responses.
    """

    github_access_token: Optional[str] = None
