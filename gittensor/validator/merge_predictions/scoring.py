# Entrius 2025

"""Pure scoring functions for merge predictions.

All functions are stateless — data in, scores out. No DB queries or side effects.

Formula per PR:
    pr_score = correctness³ * (1 + timeliness_bonus + consensus_bonus + order_bonus)

Where:
    - correctness: log-loss derived (prediction for merged, 1-prediction for non-merged), cubed
    - timeliness_bonus: 0.0-0.75, rewards early predictions (gated: raw correctness >= 0.66)
    - consensus_bonus: 0.0-0.25, rewards pre-convergence predictions (gated: raw correctness >= 0.66)
    - order_bonus: 0.0-0.75, rewards first correct predictor, merged PR only (gated: raw correctness >= 0.66)

All bonuses require raw correctness >= ORDER_CORRECTNESS_THRESHOLD to activate.
Issue score: weighted_mean × coverage, where merged PR gets weight=N, non-merged weight=1,
    and coverage = prs_predicted / total_prs.
"""

from dataclasses import dataclass
from datetime import datetime

from gittensor.constants import (
    PREDICTIONS_CORRECTNESS_EXPONENT,
    PREDICTIONS_EMA_BETA,
    PREDICTIONS_MAX_CONSENSUS_BONUS,
    PREDICTIONS_MAX_ORDER_BONUS,
    PREDICTIONS_MAX_TIMELINESS_BONUS,
    PREDICTIONS_ORDER_CORRECTNESS_THRESHOLD,
    PREDICTIONS_TIMELINESS_EXPONENT,
)


# =============================================================================
# Data structures
# =============================================================================


@dataclass
class PrPrediction:
    pr_number: int
    prediction: float  # 0.0-1.0
    prediction_time: datetime  # when this PR prediction was submitted
    variance_at_prediction: float


@dataclass
class PrOutcome:
    pr_number: int
    outcome: float  # 1.0 for merged PR, 0.0 for all others
    pr_open_time: datetime  # when this PR was opened on GitHub


@dataclass
class PrScore:
    pr_number: int
    correctness: float
    timeliness_bonus: float
    consensus_bonus: float
    order_bonus: float
    score: float  # correctness³ * (1 + timeliness + consensus + order)


@dataclass
class MinerIssueScore:
    uid: int
    pr_scores: list[PrScore]
    issue_score: float  # weighted mean (merged PR weight=N, non-merged weight=1)


# =============================================================================
# Scoring functions
# =============================================================================


def raw_correctness(prediction: float, outcome: float) -> float:
    """Log-loss derived correctness before exponentiation.

    Merged PR (outcome=1.0): score = prediction.
    Non-merged PR (outcome=0.0): score = 1 - prediction.
    """
    return prediction if outcome == 1.0 else 1.0 - prediction


def score_correctness(prediction: float, outcome: float) -> float:
    """Cubed correctness. Heavily punishes inaccuracy."""
    return raw_correctness(prediction, outcome) ** PREDICTIONS_CORRECTNESS_EXPONENT


def score_timeliness(prediction_time: datetime, settlement_time: datetime, pr_open_time: datetime) -> float:
    """Bounded timeliness bonus (0.0 to MAX_TIMELINESS_BONUS).

    Rewards earlier predictions within the PR's lifetime window.
    """
    total_window = (settlement_time - pr_open_time).total_seconds()
    if total_window <= 0:
        return 0.0

    time_remaining = (settlement_time - prediction_time).total_seconds()
    ratio = max(0.0, min(1.0, time_remaining / total_window))
    return PREDICTIONS_MAX_TIMELINESS_BONUS * ratio ** PREDICTIONS_TIMELINESS_EXPONENT


def score_consensus_bonus(
    prediction_time: datetime, peak_variance_time: datetime, settlement_time: datetime
) -> float:
    """Bounded consensus bonus (0.0 to MAX_CONSENSUS_BONUS).

    Rewards predictions made before or near peak disagreement.
    Pre-peak: full bonus. Post-peak: linearly decays to 0 at settlement.
    """
    if prediction_time <= peak_variance_time:
        return PREDICTIONS_MAX_CONSENSUS_BONUS

    remaining_window = (settlement_time - peak_variance_time).total_seconds()
    if remaining_window <= 0:
        return 0.0

    time_after_peak = (prediction_time - peak_variance_time).total_seconds()
    ratio = max(0.0, min(1.0, time_after_peak / remaining_window))
    return PREDICTIONS_MAX_CONSENSUS_BONUS * (1.0 - ratio)


def score_order_bonus(rank: int) -> float:
    """Order bonus for the merged PR only. bonus = max / rank.

    Rank 0 means unqualified (below correctness threshold). Returns 0.0.
    """
    if rank <= 0:
        return 0.0
    return PREDICTIONS_MAX_ORDER_BONUS / rank


# =============================================================================
# Order ranking (cross-miner)
# =============================================================================


def compute_merged_pr_order_ranks(
    all_miners_predictions: dict[int, list[PrPrediction]],
    merged_pr_number: int,
) -> dict[int, int]:
    """Rank miners by who first correctly predicted the merged PR.

    Only miners with raw correctness >= threshold qualify.
    Ranked by prediction_time (earliest first).

    Returns:
        dict mapping uid -> rank (1-indexed). Unqualified miners are absent.
    """
    qualifying = []

    for uid, predictions in all_miners_predictions.items():
        for pred in predictions:
            if pred.pr_number != merged_pr_number:
                continue
            rc = raw_correctness(pred.prediction, 1.0)
            if rc >= PREDICTIONS_ORDER_CORRECTNESS_THRESHOLD:
                qualifying.append((uid, pred.prediction_time))
            break

    qualifying.sort(key=lambda x: x[1])

    return {uid: rank for rank, (uid, _) in enumerate(qualifying, start=1)}


# =============================================================================
# Aggregation
# =============================================================================


def score_miner_issue(
    uid: int,
    predictions: list[PrPrediction],
    outcomes: list[PrOutcome],
    settlement_time: datetime,
    peak_variance_time: datetime,
    merged_pr_order_ranks: dict[int, int],
) -> MinerIssueScore:
    """Score a single miner's predictions for one issue.

    Fills unpredicted PRs, scores each PR, then computes a weighted issue score
    where the merged PR gets weight=N (total PRs) and non-merged get weight=1.
    """
    outcome_map = {o.pr_number: o for o in outcomes}
    merged_prs = {o.pr_number for o in outcomes if o.outcome == 1.0}
    n_prs = len(outcomes)

    miner_rank = merged_pr_order_ranks.get(uid, 0)

    pr_scores = []
    for pred in predictions:
        outcome = outcome_map.get(pred.pr_number)
        if outcome is None:
            continue

        rc = raw_correctness(pred.prediction, outcome.outcome)
        correctness = rc ** PREDICTIONS_CORRECTNESS_EXPONENT
        qualifies_for_bonus = rc >= PREDICTIONS_ORDER_CORRECTNESS_THRESHOLD

        timeliness_bonus = score_timeliness(pred.prediction_time, settlement_time, outcome.pr_open_time) if qualifies_for_bonus else 0.0
        consensus_bonus = score_consensus_bonus(pred.prediction_time, peak_variance_time, settlement_time) if qualifies_for_bonus else 0.0

        is_merged = pred.pr_number in merged_prs
        order_bonus = score_order_bonus(miner_rank) if is_merged and qualifies_for_bonus else 0.0

        score = correctness * (1.0 + timeliness_bonus + consensus_bonus + order_bonus)
        pr_scores.append(
            PrScore(
                pr_number=pred.pr_number,
                correctness=correctness,
                timeliness_bonus=timeliness_bonus,
                consensus_bonus=consensus_bonus,
                order_bonus=order_bonus,
                score=score,
            )
        )

    # Weighted mean: merged PR gets weight=N, non-merged get weight=1
    total_weight = 0.0
    weighted_sum = 0.0
    for ps in pr_scores:
        weight = n_prs if ps.pr_number in merged_prs else 1.0
        weighted_sum += ps.score * weight
        total_weight += weight

    raw_issue_score = weighted_sum / total_weight if total_weight > 0 else 0.0

    # Coverage multiplier: reward miners who reviewed the full field
    prs_predicted = len(pr_scores)
    coverage = prs_predicted / n_prs if n_prs > 0 else 0.0
    issue_score = raw_issue_score * coverage

    return MinerIssueScore(uid=uid, pr_scores=pr_scores, issue_score=issue_score)


def update_ema(current_round_score: float, previous_ema: float) -> float:
    """Exponential moving average for a miner's prediction track record."""
    return PREDICTIONS_EMA_BETA * current_round_score + (1.0 - PREDICTIONS_EMA_BETA) * previous_ema
