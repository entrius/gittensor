"""Scratch container for the mirror scoring path.

`MirrorMinerEvaluation` lives only during ``evaluate_miners_pull_requests``.
Both load and score phases write into it; ``combine`` then rolls its data
into the existing ``MinerEvaluation`` for downstream consumers.

Field names here are deliberately unprefixed (``merged_prs``, not
``mirror_merged_prs``) — within this container's namespace there's no
ambiguity. The ``mirror_*`` prefix only appears on ``MinerEvaluation`` where
both paths' data coexist.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Set

from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredMirrorPR


@dataclass
class MirrorMinerEvaluation:
    """Per-miner state collected by the mirror scoring path."""

    uid: int
    hotkey: str
    github_id: Optional[str] = None

    merged_prs: List[ScoredMirrorPR] = field(default_factory=list)
    open_prs: List[ScoredMirrorPR] = field(default_factory=list)
    closed_prs: List[ScoredMirrorPR] = field(default_factory=list)

    # Aggregate counters — rolled into MinerEvaluation totals at combine time.
    total_token_score: float = 0.0
    total_nodes_scored: int = 0
    total_structural_count: int = 0
    total_structural_score: float = 0.0
    total_leaf_count: int = 0
    total_leaf_score: float = 0.0
    total_collateral_score: float = 0.0

    # Set-union into MinerEvaluation.unique_repos_contributed_to at combine time.
    unique_repos_contributed_to: Set[str] = field(default_factory=set)

    # OR'd into MinerEvaluation.github_pr_fetch_failed at combine time.
    fetch_failed: bool = False

    @property
    def total_merged_prs(self) -> int:
        return len(self.merged_prs)

    @property
    def total_open_prs(self) -> int:
        return len(self.open_prs)

    @property
    def total_closed_prs(self) -> int:
        return len(self.closed_prs)
