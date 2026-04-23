"""Merge a `MirrorMinerEvaluation` into the legacy `MinerEvaluation`.

Single explicit join point between the two scoring paths. Mirror PR lists go
into `mirror_*` slots; aggregate counters sum into legacy_eval's totals; the
unique-repos set is unioned; `github_pr_fetch_failed` is OR'd.

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

    legacy_eval.total_token_score += mirror_eval.total_token_score
    legacy_eval.total_nodes_scored += mirror_eval.total_nodes_scored
    legacy_eval.total_structural_count += mirror_eval.total_structural_count
    legacy_eval.total_structural_score += mirror_eval.total_structural_score
    legacy_eval.total_leaf_count += mirror_eval.total_leaf_count
    legacy_eval.total_leaf_score += mirror_eval.total_leaf_score
    legacy_eval.total_collateral_score += mirror_eval.total_collateral_score

    legacy_eval.unique_repos_contributed_to |= mirror_eval.unique_repos_contributed_to
    legacy_eval.unique_repos_count = len(legacy_eval.unique_repos_contributed_to)

    legacy_eval.github_pr_fetch_failed = (
        legacy_eval.github_pr_fetch_failed or mirror_eval.fetch_failed
    )

    return legacy_eval
