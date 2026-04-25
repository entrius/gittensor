"""Merge a `MirrorMinerEvaluation` into the legacy `MinerEvaluation`.

Single explicit join point between the two scoring paths:
- Mirror PR lists land in the ``mirror_*`` slots on the MinerEvaluation
- ``unique_repos_contributed_to`` is unioned
- ``github_pr_fetch_failed`` is OR'd

Per-PR scoring breakdowns (token_score, nodes_scored, base_score, earned_score,
collateral_score) live on each ScoredMirrorPR — they get aggregated into
MinerEvaluation totals by ``finalize_miner_scores`` walking both paths' lists,
not at combine time.

Returning the same `MinerEvaluation` (mutated in place) keeps downstream
signatures unchanged. On delete-day this whole module goes away — at that
point `MirrorMinerEvaluation` becomes the canonical container.
"""

from gittensor.classes import MinerEvaluation
from gittensor.validator.oss_contributions.mirror.evaluation import MirrorMinerEvaluation


def combine(legacy_eval: MinerEvaluation, mirror_eval: MirrorMinerEvaluation) -> MinerEvaluation:
    """Roll mirror_eval into legacy_eval, returning the merged MinerEvaluation."""

    legacy_eval.mirror_merged_prs = mirror_eval.merged_prs
    legacy_eval.mirror_open_prs = mirror_eval.open_prs
    legacy_eval.mirror_closed_prs = mirror_eval.closed_prs

    legacy_eval.unique_repos_contributed_to |= mirror_eval.unique_repos_contributed_to
    legacy_eval.unique_repos_count = len(legacy_eval.unique_repos_contributed_to)

    legacy_eval.github_pr_fetch_failed = (
        legacy_eval.github_pr_fetch_failed or mirror_eval.fetch_failed
    )

    return legacy_eval
