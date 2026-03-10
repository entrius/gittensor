# Entrius 2025

"""Pure input validation for prediction payloads."""

from gittensor.constants import (
    PREDICTIONS_MAX_VALUE,
    PREDICTIONS_MIN_VALUE,
)


def validate_prediction_values(predictions: dict[int, float]) -> str | None:
    """Validate prediction payload structure and values."""
    if not predictions:
        return 'Empty predictions'

    for pr_number, value in predictions.items():
        if not isinstance(pr_number, int) or pr_number <= 0:
            return f'Invalid PR number: {pr_number}'
        if not (PREDICTIONS_MIN_VALUE <= value <= PREDICTIONS_MAX_VALUE):
            return f'Prediction for PR #{pr_number} out of range: {value} (must be {PREDICTIONS_MIN_VALUE}-{PREDICTIONS_MAX_VALUE})'

    total = sum(predictions.values())
    if total > 1.0:
        return f'Submission total exceeds 1.0: {total:.4f}'

    return None
