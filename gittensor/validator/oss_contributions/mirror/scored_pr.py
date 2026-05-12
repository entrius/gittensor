"""Per-PR container for the mirror scoring path.

`ScoredMirrorPR` wraps a `MirrorPullRequest` (raw mirror response data) with
the scoring fields populated by `score_mirror_miner_prs`. Composition over
inheritance — raw response data is accessed via ``scored.pr.<field>`` so
``gittensor.utils.mirror.models`` stays scoring-agnostic and fully reusable.

The scoring fields mirror the equivalents on the legacy ``PullRequest``
dataclass so downstream math (``calculate_final_earned_score``,
``is_pioneer_eligible``) translates cleanly.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from gittensor.classes import _apply_score_multipliers
from gittensor.constants import MIN_TOKEN_SCORE_FOR_BASE_SCORE
from gittensor.utils.mirror.models import MirrorFile, MirrorPullRequest


@dataclass
class ScoredMirrorPR:
    """A `MirrorPullRequest` plus the scoring state derived during evaluation."""

    pr: MirrorPullRequest

    # Multipliers (default 1.0 — neutral if not yet computed)
    repo_weight_multiplier: float = 1.0
    issue_multiplier: float = 1.0
    open_pr_spam_multiplier: float = 1.0
    time_decay_multiplier: float = 1.0
    credibility_multiplier: float = 1.0
    review_quality_multiplier: float = 1.0
    label_multiplier: float = 1.0
    label: Optional[str] = None

    # Pioneer attribution (per-repo, populated post per-PR scoring)
    pioneer_dividend: float = 0.0
    pioneer_rank: int = 0  # 0 = not eligible, 1 = pioneer, 2+ = follower position

    # Score outputs
    base_score: float = 0.0
    earned_score: float = 0.0
    collateral_score: float = 0.0  # OPEN PRs only

    # Token scoring breakdown (populated when files are tokenized)
    code_density: float = 0.0
    token_score: float = 0.0
    structural_count: int = 0
    structural_score: float = 0.0
    leaf_count: int = 0
    leaf_score: float = 0.0
    total_nodes_scored: int = 0

    # Files fetched lazily via MirrorClient.get_pr_files for eligible PRs
    files: Optional[List[MirrorFile]] = None

    @property
    def number(self) -> int:
        """Alias for ``self.pr.pr_number`` — enables duck-typing with legacy
        PullRequest so source-agnostic functions (e.g. ``calculate_open_pr_collateral_score``)
        accept a ScoredMirrorPR without modification."""
        return self.pr.pr_number

    @property
    def repository_full_name(self) -> str:
        """Alias for ``self.pr.repo_full_name`` — matches legacy PullRequest
        attribute name for duck-typing purposes."""
        return self.pr.repo_full_name

    @property
    def changes_requested_count(self) -> int:
        """Alias for the maintainer-only CHANGES_REQUESTED count used by
        source-agnostic scoring helpers."""
        return self.pr.review_summary.maintainer_changes_requested_count

    @property
    def merged_at(self) -> Optional[datetime]:
        """Alias for ``self.pr.merged_at`` — matches legacy PullRequest attribute
        name so the unified pioneer-dividend walk treats both types identically."""
        return self.pr.merged_at

    def is_pioneer_eligible(self) -> bool:
        """Pioneer-eligible iff merged AND meets the minimum token-score gate.

        Mirrors `PullRequest.is_pioneer_eligible` so the legacy pioneer math
        functions can be reused unchanged.
        """
        return self.pr.merged_at is not None and self.token_score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE

    def calculate_final_earned_score(self) -> float:
        """Combine base score with all multipliers. Pioneer dividend is added separately after."""
        multipliers = {
            'issue': self.issue_multiplier,
            'label': self.label_multiplier,
            'spam': self.open_pr_spam_multiplier,
            'decay': self.time_decay_multiplier,
            'cred': self.credibility_multiplier,
            'review': self.review_quality_multiplier,
        }
        label = f'{self.pr.state} PR #{self.pr.pr_number} ({self.pr.repo_full_name})'
        self.earned_score = _apply_score_multipliers(self.base_score, multipliers, label)
        return self.earned_score
