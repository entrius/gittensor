# Entrius 2025

"""Pure scoring functions for merge predictions.

All functions are stateless — data in, scores out. No DB queries or side effects.
"""

from dataclasses import dataclass
from datetime import datetime

from gittensor.constants import PREDICTIONS_EMA_BETA, PREDICTIONS_TIMELINESS_EXPONENT


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
    timeliness: float
    consensus_bonus: float
    score: float  # correctness * timeliness * (1 + consensus_bonus)


@dataclass
class MinerIssueScore:
    uid: int
    pr_scores: list[PrScore]
    issue_score: float  # mean(pr_scores)


# =============================================================================
# Scoring functions
# =============================================================================


def score_correctness(prediction: float, outcome: float) -> float:
    """1 - (prediction - outcome)^2. Perfect prediction = 1.0, worst = 0.0."""
    return 1.0 - (prediction - outcome) ** 2


def score_timeliness(prediction_time: datetime, settlement_time: datetime, pr_open_time: datetime) -> float:
    """Reward earlier predictions. ratio^exponent where ratio = time_remaining / total_window."""
    total_window = (settlement_time - pr_open_time).total_seconds()
    if total_window <= 0:
        return 0.0

    time_remaining = (settlement_time - prediction_time).total_seconds()
    ratio = max(0.0, min(1.0, time_remaining / total_window))
    return ratio**PREDICTIONS_TIMELINESS_EXPONENT


def score_consensus_bonus(
    prediction_time: datetime, peak_variance_time: datetime, settlement_time: datetime
) -> float:
    """Reward predictions made before or near peak disagreement.

    Pre-peak: full bonus (1.0).
    Post-peak: linearly decays to 0 at settlement.
    """
    if prediction_time <= peak_variance_time:
        return 1.0

    remaining_window = (settlement_time - peak_variance_time).total_seconds()
    if remaining_window <= 0:
        return 0.0

    time_after_peak = (prediction_time - peak_variance_time).total_seconds()
    ratio = max(0.0, min(1.0, time_after_peak / remaining_window))
    return 1.0 - ratio


# =============================================================================
# Aggregation
# =============================================================================


def fill_unpredicted_prs(
    predictions: list[PrPrediction],
    all_pr_numbers: list[int],
    settlement_time: datetime,
) -> list[PrPrediction]:
    """Fill in missing PR predictions so every miner covers every PR.

    Unallocated probability is spread uniformly across unpredicted PRs.
    Filled predictions get settlement_time as their timestamp (worst timeliness).
    """
    predicted_prs = {p.pr_number for p in predictions}
    missing_prs = [pr for pr in all_pr_numbers if pr not in predicted_prs]

    if not missing_prs:
        return list(predictions)

    allocated = sum(p.prediction for p in predictions)
    unallocated = max(0.0, 1.0 - allocated)
    fill_value = unallocated / len(missing_prs)

    filled = list(predictions)
    for pr_number in missing_prs:
        filled.append(
            PrPrediction(
                pr_number=pr_number,
                prediction=fill_value,
                prediction_time=settlement_time,
                variance_at_prediction=0.0,
            )
        )

    return filled


def score_miner_issue(
    uid: int,
    predictions: list[PrPrediction],
    outcomes: list[PrOutcome],
    settlement_time: datetime,
    peak_variance_time: datetime,
) -> MinerIssueScore:
    """Score a single miner's predictions for one issue.

    Fills unpredicted PRs, then scores each PR and averages.
    """
    all_pr_numbers = [o.pr_number for o in outcomes]
    outcome_map = {o.pr_number: o for o in outcomes}

    full_predictions = fill_unpredicted_prs(predictions, all_pr_numbers, settlement_time)

    pr_scores = []
    for pred in full_predictions:
        outcome = outcome_map.get(pred.pr_number)
        if outcome is None:
            continue

        correctness = score_correctness(pred.prediction, outcome.outcome)
        timeliness = score_timeliness(pred.prediction_time, settlement_time, outcome.pr_open_time)
        consensus_bonus = score_consensus_bonus(pred.prediction_time, peak_variance_time, settlement_time)

        score = correctness * timeliness * (1.0 + consensus_bonus)
        pr_scores.append(
            PrScore(
                pr_number=pred.pr_number,
                correctness=correctness,
                timeliness=timeliness,
                consensus_bonus=consensus_bonus,
                score=score,
            )
        )

    issue_score = sum(ps.score for ps in pr_scores) / len(pr_scores) if pr_scores else 0.0

    return MinerIssueScore(uid=uid, pr_scores=pr_scores, issue_score=issue_score)


def update_ema(current_round_score: float, previous_ema: float) -> float:
    """Exponential moving average for a miner's prediction track record."""
    return PREDICTIONS_EMA_BETA * current_round_score + (1.0 - PREDICTIONS_EMA_BETA) * previous_ema
