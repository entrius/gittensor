from dataclasses import dataclass, field
from datetime import datetime
from typing import DefaultDict, List, Optional, Set, Dict
from gittensor.utils.utils import mask_secret

import bittensor as bt

from gittensor.constants import (
    EXCESSIVE_PR_MIN_WEIGHT,
    EXCESSIVE_PR_PENALTY_SLOPE,
    EXCESSIVE_PR_PENALTY_THRESHOLD,
    DEFAULT_PROGRAMMING_LANGUAGE_WEIGHT,
    MITIGATED_EXTENSIONS,
    MAX_LINES_SCORED_CHANGES,
)

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
    author_login: Optional[str] = None
    state: Optional[str] = None  # "OPEN" or "CLOSED"


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
    description: Optional[str] = None
    last_edited_at: Optional[datetime] = None
    gittensor_tagged: bool = False
    total_lines_scored: int = 0
    penalty_list = []

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
        
    def set_applied_penalties(self, penalties) -> None:
        """Set the penalties applied for this pull request"""
        self.penalty_list = penalties

    def calculate_score_from_file_changes(self, programming_languages: Dict[str, float]):
        """
        Calculate the score for a single PR based on its file changes.

        Args:
            programming_languages (Dict[str, float]): List of programming language weights
        """

        if not self.file_changes:
            self.earned_score = 0.0

        total_file_changes = sum(file_change.changes for file_change in self.file_changes)
        total_lines_scored = 0
        pr_score = 0.0

        for file in self.file_changes:
            language_weight = programming_languages.get(file.file_extension, DEFAULT_PROGRAMMING_LANGUAGE_WEIGHT)

            actual_changes = file.changes

            # Cap scored changes for extensions that are exploitable
            scored_changes = actual_changes
            if file.file_extension in MITIGATED_EXTENSIONS:
                scored_changes = min(actual_changes, MAX_LINES_SCORED_CHANGES)

            total_lines_scored += scored_changes

            # Normalized by total changes in the PR
            weight_ratio = actual_changes / total_file_changes if total_file_changes > 0 else 0
            pr_score += language_weight * weight_ratio * (scored_changes**0.75)

        self.total_lines_scored = total_lines_scored
        return pr_score
    
    def get_dominant_file_type(self) -> Optional[str]:
        """
        Determine the dominant file type category in this PR.
        
        Returns:
            str: The dominant file type ('test', 'doc', 'translation')
                 or None if no clear dominant type
        """
        from gittensor.constants import SPAM_FILE_TYPE_PATTERNS
        
        if not self.file_changes:
            return None
        
        # Count changes per file type category
        type_changes = {file_type: 0 for file_type in SPAM_FILE_TYPE_PATTERNS.keys()}
        type_changes['other'] = 0
        total_changes = 0
        
        for file_change in self.file_changes:
            filename_lower = file_change.filename.lower()
            total_changes += file_change.changes
            
            # Check which category this file belongs to
            categorized = False
            for file_type, patterns in SPAM_FILE_TYPE_PATTERNS.items():
                if any(pattern in filename_lower for pattern in patterns):
                    type_changes[file_type] += file_change.changes
                    categorized = True
                    break
            
            if not categorized:
                type_changes['other'] += file_change.changes
        
        if total_changes == 0:
            return None
        
        # Find dominant type (must be >60% of changes)
        for file_type, changes in type_changes.items():
            if file_type != 'other' and changes / total_changes > 0.6:
                return file_type
        
        return None

    @classmethod
    def from_graphql_response(cls, pr_data: dict, uid: int, hotkey: str, github_id: str) -> 'PullRequest':
        """Create PullRequest from GraphQL API response"""
        # Import here to avoid circular dependency
        from gittensor.validator.utils.datetime_utils import parse_github_timestamp
        from gittensor.constants import PR_TAGLINE

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
                        f"PR #{mask_secret(str(pr_data['number']))} in {mask_secret(repository_full_name)} has Gittensor tagline but was edited after merge "
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
            additions=pr_data['additions'],
            deletions=pr_data['deletions'],
            commits=pr_data.get('commits', {}).get('totalCount', 0),
            merged_by_login=pr_data['mergedBy']['login'] if pr_data.get('mergedBy') else None,
            issues=issues,
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

        self.total_lines_changed = sum(pr.total_lines_scored for pr in self.pull_requests)
        self.unique_repos_contributed_to = set(pr.repository_full_name for pr in self.pull_requests)
        self.unique_repos_count = len(self.unique_repos_contributed_to)

        bt.logging.info(f"Final evaluation for UID {self.uid}:")
        bt.logging.info(f"  - Total Score: {self.total_score:.5f}")
        bt.logging.info(f"  - Total Valid PRs: {self.total_prs}")
        bt.logging.info(f"  - Total open PRs: {self.total_open_prs}")
        bt.logging.info(f"  - Total Lines Changed (& Scored): {self.total_lines_changed}")
        bt.logging.info(f"  - Unique Repositories Contributed To: {self.get_unique_repositories()}")

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
        
    def analyze_repetitive_spam_per_repository(self) -> Dict[str, Dict[str, any]]:
        """
        Analyze if miner is submitting repetitive spam PRs to specific repositories.
        
        Groups PRs by repository and checks if they're all the same type
        (e.g., all tests, all docs, all config files).
        
        Returns:
            Dict mapping repository -> {
                'total_prs': int,
                'dominant_type': str,
                'type_ratio': float,
                'is_spam': bool
            }
        """
        from collections import defaultdict
        from gittensor.constants import (
            REPETITIVE_SPAM_MIN_PRS,
            REPETITIVE_SPAM_THRESHOLD
        )
        
        # Group PRs by repository
        repo_prs = defaultdict(list)
        for pr in self.pull_requests:
            repo_prs[pr.repository_full_name].append(pr)
        
        analysis = {}
        
        for repo, prs in repo_prs.items():
            # Need minimum PRs to detect pattern
            if len(prs) < REPETITIVE_SPAM_MIN_PRS:
                continue
            
            # Count file types across all PRs in this repo
            type_counts = defaultdict(int)
            
            for pr in prs:
                dominant_type = pr.get_dominant_file_type()
                if dominant_type:
                    type_counts[dominant_type] += 1
            
            if not type_counts:
                continue
            
            # Find most common type
            most_common_type = max(type_counts.items(), key=lambda x: x[1])
            dominant_type = most_common_type[0]
            dominant_count = most_common_type[1]
            
            type_ratio = dominant_count / len(prs)
            is_spam = type_ratio >= REPETITIVE_SPAM_THRESHOLD
            
            analysis[repo] = {
                'total_prs': len(prs),
                'dominant_type': dominant_type,
                'type_ratio': type_ratio,
                'is_spam': is_spam,
                'pr_numbers': [pr.number for pr in prs]
            }
        
        return analysis


class GitPatSynapse(bt.Synapse):
    """
    This synapse is used to request GitHub access tokens from a miner and receive the response.

    Attributes:
    - github_access_token: A string value representing the GitHub access token.
      Initially None for requests, and set to the actual token for responses.
    """

    github_access_token: Optional[str] = None
