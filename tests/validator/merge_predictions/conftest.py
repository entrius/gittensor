# Entrius 2025

"""Shared fixtures for merge predictions tests."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from bittensor.core.synapse import TerminalInfo

from gittensor.synapses import PredictionSynapse
from gittensor.validator.merge_predictions.mp_storage import PredictionStorage
from gittensor.validator.merge_predictions.scoring import PrOutcome, PrPrediction

# ============================================================================
# Storage
# ============================================================================


@pytest.fixture
def mp_storage(tmp_path):
    """Real SQLite-backed PredictionStorage in a temp directory."""
    return PredictionStorage(db_path=str(tmp_path / 'test.db'))


# ============================================================================
# Time helpers
# ============================================================================


@pytest.fixture
def base_times():
    """Spread of datetimes across a 30-day window for scoring tests."""
    pr_open = datetime(2025, 6, 1, tzinfo=timezone.utc)
    return {
        'pr_open': pr_open,
        'peak_variance': pr_open + timedelta(days=10),
        'prediction_early': pr_open + timedelta(days=2),
        'prediction_mid': pr_open + timedelta(days=15),
        'prediction_late': pr_open + timedelta(days=28),
        'settlement': pr_open + timedelta(days=30),
    }


# ============================================================================
# Outcomes & Predictions
# ============================================================================


@pytest.fixture
def sample_outcomes(base_times):
    """4 PRs: #1 merged, #2-#4 non-merged."""
    pr_open = base_times['pr_open']
    return [
        PrOutcome(pr_number=1, outcome=1.0, pr_open_time=pr_open),
        PrOutcome(pr_number=2, outcome=0.0, pr_open_time=pr_open),
        PrOutcome(pr_number=3, outcome=0.0, pr_open_time=pr_open),
        PrOutcome(pr_number=4, outcome=0.0, pr_open_time=pr_open),
    ]


@pytest.fixture
def alice_predictions(base_times):
    """Early + accurate miner: high on merged PR, low on others."""
    t = base_times['prediction_early']
    return [
        PrPrediction(pr_number=1, prediction=0.70, prediction_time=t, variance_at_prediction=0.05),
        PrPrediction(pr_number=2, prediction=0.15, prediction_time=t, variance_at_prediction=0.05),
        PrPrediction(pr_number=3, prediction=0.10, prediction_time=t, variance_at_prediction=0.05),
        PrPrediction(pr_number=4, prediction=0.05, prediction_time=t, variance_at_prediction=0.05),
    ]


@pytest.fixture
def dave_predictions(base_times):
    """Spray-and-pray miner: uniform 0.25 across all PRs."""
    t = base_times['prediction_early']
    return [
        PrPrediction(pr_number=1, prediction=0.25, prediction_time=t, variance_at_prediction=0.05),
        PrPrediction(pr_number=2, prediction=0.25, prediction_time=t, variance_at_prediction=0.05),
        PrPrediction(pr_number=3, prediction=0.25, prediction_time=t, variance_at_prediction=0.05),
        PrPrediction(pr_number=4, prediction=0.25, prediction_time=t, variance_at_prediction=0.05),
    ]


# ============================================================================
# Validator mock
# ============================================================================


@pytest.fixture
def mock_validator(mp_storage):
    """MagicMock validator with mp_storage, metagraph, and subtensor."""
    v = MagicMock()
    v.mp_storage = mp_storage

    # metagraph with 3 registered hotkeys
    v.metagraph.hotkeys = ['hk_alice', 'hk_bob', 'hk_charlie']
    v.metagraph.S = [100.0, 50.0, 25.0]

    v.subtensor = MagicMock()
    return v


# ============================================================================
# Synapse factory
# ============================================================================


@pytest.fixture
def make_synapse():
    """Factory that builds a PredictionSynapse with configurable fields."""

    def _make(
        predictions=None,
        issue_id=1,
        repository='test/repo',
        github_access_token='ghp_test123',
        hotkey='hk_alice',
    ):
        synapse = PredictionSynapse(
            predictions=predictions or {1: 0.5},
            issue_id=issue_id,
            repository=repository,
            github_access_token=github_access_token,
        )
        synapse.dendrite = TerminalInfo(hotkey=hotkey)
        return synapse

    return _make
