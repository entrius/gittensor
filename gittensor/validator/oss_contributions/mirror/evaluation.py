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
    # Storage-only: persisted to flip pr_state OPEN→CLOSED, excluded from
    # scoring. Mirror parity for stale_closed_pull_requests (see #769).
    stale_closed_prs: List[ScoredMirrorPR] = field(default_factory=list)

    # Set-union into MinerEvaluation.unique_repos_contributed_to at combine
    # time. Populated by score_mirror_pr as it processes each MERGED PR.
    unique_repos_contributed_to: Set[str] = field(default_factory=set)

    # OR'd into MinerEvaluation.github_pr_fetch_failed at combine time. Set
    # by load_mirror_miner_prs when MirrorClient raises MirrorRequestError.
    fetch_failed: bool = False

    # Per-PR token-scoring breakdowns (token_score, nodes_scored, structural_*,
    # leaf_*) and collateral_score live on each ScoredMirrorPR and are aggregated
    # into MinerEvaluation totals during finalize_miner_scores — not at combine
    # time and not on this container.

    @property
    def total_merged_prs(self) -> int:
        return len(self.merged_prs)

    @property
    def total_open_prs(self) -> int:
        return len(self.open_prs)

    @property
    def total_closed_prs(self) -> int:
        return len(self.closed_prs)
