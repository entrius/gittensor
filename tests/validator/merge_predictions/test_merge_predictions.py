# Entrius 2025

"""Merge predictions test suite.

Covers: storage, handler, scoring, validation, and settlement.

Run:
    pytest tests/validator/merge_predictions/ -v
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from gittensor.constants import (
    PREDICTIONS_COOLDOWN_SECONDS,
    PREDICTIONS_CORRECTNESS_EXPONENT,
    PREDICTIONS_EMA_BETA,
    PREDICTIONS_MAX_CONSENSUS_BONUS,
    PREDICTIONS_MAX_ORDER_BONUS,
    PREDICTIONS_MAX_TIMELINESS_BONUS,
    PREDICTIONS_TIMELINESS_EXPONENT,
)
from gittensor.validator.merge_predictions.scoring import (
    MinerIssueScore,
    PrOutcome,
    PrPrediction,
    compute_merged_pr_order_ranks,
    score_consensus_bonus,
    score_correctness,
    score_miner_issue,
    score_order_bonus,
    score_timeliness,
    update_ema,
)
from gittensor.validator.merge_predictions.validation import validate_prediction_values


def _run(coro):
    """Run an async coroutine synchronously (no pytest-asyncio needed)."""
    return asyncio.run(coro)


# =============================================================================
# 1. Storage
# =============================================================================


class TestPredictionStorage:
    """Tests for PredictionStorage (real SQLite, no mocking)."""

    def test_tables_created(self, mp_storage):
        with mp_storage._get_connection() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert 'predictions' in tables
        assert 'prediction_emas' in tables
        assert 'settled_issues' in tables

    def test_store_and_retrieve_prediction(self, mp_storage):
        mp_storage.store_prediction(
            uid=0, hotkey='hk', github_id='gh1', issue_id=1,
            repository='r/r', pr_number=10, prediction=0.6, variance_at_prediction=0.1,
        )
        rows = mp_storage.get_predictions_for_issue(1)
        assert len(rows) == 1
        assert rows[0]['prediction'] == pytest.approx(0.6)
        assert rows[0]['pr_number'] == 10

    def test_upsert_replaces_prediction(self, mp_storage):
        kwargs = dict(uid=0, hotkey='hk', github_id='gh1', issue_id=1, repository='r/r', pr_number=10)
        mp_storage.store_prediction(**kwargs, prediction=0.3, variance_at_prediction=0.1)
        mp_storage.store_prediction(**kwargs, prediction=0.8, variance_at_prediction=0.2)
        rows = mp_storage.get_predictions_for_issue(1)
        assert len(rows) == 1
        assert rows[0]['prediction'] == pytest.approx(0.8)

    def test_upsert_preserves_other_prs(self, mp_storage):
        base = dict(uid=0, hotkey='hk', github_id='gh1', issue_id=1, repository='r/r')
        mp_storage.store_prediction(**base, pr_number=1, prediction=0.3, variance_at_prediction=0.0)
        mp_storage.store_prediction(**base, pr_number=2, prediction=0.4, variance_at_prediction=0.0)

        # Update only PR #1
        mp_storage.store_prediction(**base, pr_number=1, prediction=0.5, variance_at_prediction=0.0)

        rows = mp_storage.get_predictions_for_issue(1)
        by_pr = {r['pr_number']: r for r in rows}
        assert by_pr[1]['prediction'] == pytest.approx(0.5)
        assert by_pr[2]['prediction'] == pytest.approx(0.4)

    def test_miner_total_for_issue(self, mp_storage):
        base = dict(uid=0, hotkey='hk', github_id='gh1', issue_id=1, repository='r/r')
        mp_storage.store_prediction(**base, pr_number=1, prediction=0.3, variance_at_prediction=0.0)
        mp_storage.store_prediction(**base, pr_number=2, prediction=0.4, variance_at_prediction=0.0)
        total = mp_storage.get_miner_total_for_issue(0, 'hk', 1)
        assert total == pytest.approx(0.7)

    def test_miner_total_excludes_pr(self, mp_storage):
        base = dict(uid=0, hotkey='hk', github_id='gh1', issue_id=1, repository='r/r')
        mp_storage.store_prediction(**base, pr_number=1, prediction=0.3, variance_at_prediction=0.0)
        mp_storage.store_prediction(**base, pr_number=2, prediction=0.4, variance_at_prediction=0.0)
        total = mp_storage.get_miner_total_for_issue(0, 'hk', 1, exclude_pr=2)
        assert total == pytest.approx(0.3)

    def test_cooldown_active(self, mp_storage):
        mp_storage.store_prediction(
            uid=0, hotkey='hk', github_id='gh1', issue_id=1,
            repository='r/r', pr_number=1, prediction=0.5, variance_at_prediction=0.0,
        )
        remaining = mp_storage.check_cooldown(0, 'hk', 1, 1)
        assert remaining is not None
        assert remaining > 0

    def test_cooldown_expired(self, mp_storage):
        """Store a prediction with a timestamp far in the past, then verify cooldown is None."""
        # Insert directly with an old timestamp to avoid patching datetime
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=PREDICTIONS_COOLDOWN_SECONDS + 60)).isoformat()
        with mp_storage._get_connection() as conn:
            conn.execute(
                'INSERT INTO predictions (uid, hotkey, github_id, issue_id, repository, pr_number, prediction, timestamp, variance_at_prediction) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (0, 'hk', 'gh1', 1, 'r/r', 1, 0.5, old_ts, 0.0),
            )
            conn.commit()

        remaining = mp_storage.check_cooldown(0, 'hk', 1, 1)
        assert remaining is None

    def test_cooldown_no_prior_prediction(self, mp_storage):
        assert mp_storage.check_cooldown(0, 'hk', 1, 1) is None

    def test_compute_variance_single_miner(self, mp_storage):
        mp_storage.store_prediction(
            uid=0, hotkey='hk', github_id='gh1', issue_id=1,
            repository='r/r', pr_number=1, prediction=0.5, variance_at_prediction=0.0,
        )
        assert mp_storage.compute_current_variance(1) == pytest.approx(0.0)

    def test_compute_variance_disagreement(self, mp_storage):
        base = dict(github_id='gh1', issue_id=1, repository='r/r', pr_number=1)
        mp_storage.store_prediction(uid=0, hotkey='hk0', **base, prediction=0.9, variance_at_prediction=0.0)
        mp_storage.store_prediction(uid=1, hotkey='hk1', **base, prediction=0.1, variance_at_prediction=0.0)
        var = mp_storage.compute_current_variance(1)
        # var((0.9,0.1)) = mean(x^2) - mean(x)^2 = 0.41 - 0.25 = 0.16
        assert var > 0

    def test_peak_variance_time(self, mp_storage):
        base = dict(uid=0, hotkey='hk', github_id='gh1', issue_id=1, repository='r/r')
        mp_storage.store_prediction(**base, pr_number=1, prediction=0.5, variance_at_prediction=0.1)
        mp_storage.store_prediction(**base, pr_number=2, prediction=0.5, variance_at_prediction=0.9)
        peak = mp_storage.get_peak_variance_time(1)
        assert peak is not None

    def test_ema_default_zero(self, mp_storage):
        assert mp_storage.get_ema('unknown_github_id') == 0.0

    def test_ema_upsert_increments_rounds(self, mp_storage):
        mp_storage.update_ema('gh1', 0.5)
        mp_storage.update_ema('gh1', 0.6)
        emas = mp_storage.get_all_emas()
        by_id = {e['github_id']: e for e in emas}
        assert by_id['gh1']['rounds'] == 2

    def test_get_all_emas(self, mp_storage):
        mp_storage.update_ema('gh1', 0.5)
        mp_storage.update_ema('gh2', 0.3)
        emas = mp_storage.get_all_emas()
        ids = {e['github_id'] for e in emas}
        assert ids == {'gh1', 'gh2'}

    def test_delete_predictions_for_issue(self, mp_storage):
        base = dict(uid=0, hotkey='hk', github_id='gh1', issue_id=1, repository='r/r')
        mp_storage.store_prediction(**base, pr_number=1, prediction=0.3, variance_at_prediction=0.0)
        mp_storage.store_prediction(**base, pr_number=2, prediction=0.4, variance_at_prediction=0.0)
        deleted = mp_storage.delete_predictions_for_issue(1)
        assert deleted == 2
        assert mp_storage.get_predictions_for_issue(1) == []

    def test_delete_predictions_no_rows(self, mp_storage):
        deleted = mp_storage.delete_predictions_for_issue(999)
        assert deleted == 0

    def test_mark_and_check_settled(self, mp_storage):
        mp_storage.mark_issue_settled(42, 'scored', merged_pr_number=7)
        assert mp_storage.is_issue_settled(42) is True

    def test_is_issue_settled_false(self, mp_storage):
        assert mp_storage.is_issue_settled(999) is False

    def test_mark_settled_voided(self, mp_storage):
        mp_storage.mark_issue_settled(10, 'voided')
        assert mp_storage.is_issue_settled(10) is True

    def test_mark_settled_idempotent(self, mp_storage):
        mp_storage.mark_issue_settled(42, 'scored', merged_pr_number=7)
        mp_storage.mark_issue_settled(42, 'scored', merged_pr_number=7)
        assert mp_storage.is_issue_settled(42) is True


# =============================================================================
# 2. Handler
# =============================================================================


class TestPredictionHandler:
    """Tests for handle_prediction, blacklist_prediction, priority_prediction."""

    @patch('gittensor.validator.merge_predictions.handler.validate_prediction_values', return_value=None)
    @patch('gittensor.validator.merge_predictions.handler.validate_github_credentials', return_value=('gh_alice', None))
    @patch('gittensor.validator.merge_predictions.handler.check_prs_open', return_value=None)
    @patch('gittensor.validator.merge_predictions.handler.check_issue_active', return_value=None)
    def test_successful_prediction_stored(self, _cia, _cpo, _vgc, _vpv, mock_validator, make_synapse):
        from gittensor.validator.merge_predictions.handler import handle_prediction

        synapse = make_synapse(predictions={1: 0.5}, hotkey='hk_alice')
        result = _run(handle_prediction(mock_validator, synapse))
        assert result.accepted is True
        rows = mock_validator.mp_storage.get_predictions_for_issue(1)
        assert len(rows) == 1

    @patch('gittensor.validator.merge_predictions.handler.check_issue_active', return_value='Issue not found')
    def test_reject_inactive_issue(self, _cia, mock_validator, make_synapse):
        from gittensor.validator.merge_predictions.handler import handle_prediction

        synapse = make_synapse(hotkey='hk_alice')
        result = _run(handle_prediction(mock_validator, synapse))
        assert result.accepted is False
        assert 'Issue not found' in result.rejection_reason

    @patch('gittensor.validator.merge_predictions.handler.check_prs_open', return_value='PR #1 is not open')
    @patch('gittensor.validator.merge_predictions.handler.check_issue_active', return_value=None)
    def test_reject_closed_pr(self, _cia, _cpo, mock_validator, make_synapse):
        from gittensor.validator.merge_predictions.handler import handle_prediction

        synapse = make_synapse(hotkey='hk_alice')
        result = _run(handle_prediction(mock_validator, synapse))
        assert result.accepted is False
        assert 'not open' in result.rejection_reason

    @patch('gittensor.validator.merge_predictions.handler.validate_github_credentials', return_value=(None, 'Bad PAT'))
    @patch('gittensor.validator.merge_predictions.handler.check_prs_open', return_value=None)
    @patch('gittensor.validator.merge_predictions.handler.check_issue_active', return_value=None)
    def test_reject_invalid_github_creds(self, _cia, _cpo, _vgc, mock_validator, make_synapse):
        from gittensor.validator.merge_predictions.handler import handle_prediction

        synapse = make_synapse(hotkey='hk_alice')
        result = _run(handle_prediction(mock_validator, synapse))
        assert result.accepted is False
        assert 'Bad PAT' in result.rejection_reason

    @patch('gittensor.validator.merge_predictions.handler.validate_prediction_values', return_value='Values bad')
    @patch('gittensor.validator.merge_predictions.handler.validate_github_credentials', return_value=('gh_alice', None))
    @patch('gittensor.validator.merge_predictions.handler.check_prs_open', return_value=None)
    @patch('gittensor.validator.merge_predictions.handler.check_issue_active', return_value=None)
    def test_reject_invalid_values(self, _cia, _cpo, _vgc, _vpv, mock_validator, make_synapse):
        from gittensor.validator.merge_predictions.handler import handle_prediction

        synapse = make_synapse(hotkey='hk_alice')
        result = _run(handle_prediction(mock_validator, synapse))
        assert result.accepted is False
        assert 'Values bad' in result.rejection_reason

    @patch('gittensor.validator.merge_predictions.handler.validate_prediction_values', return_value=None)
    @patch('gittensor.validator.merge_predictions.handler.validate_github_credentials', return_value=('gh_alice', None))
    @patch('gittensor.validator.merge_predictions.handler.check_prs_open', return_value=None)
    @patch('gittensor.validator.merge_predictions.handler.check_issue_active', return_value=None)
    def test_reject_cooldown(self, _cia, _cpo, _vgc, _vpv, mock_validator, make_synapse):
        from gittensor.validator.merge_predictions.handler import handle_prediction

        # First prediction succeeds
        s1 = make_synapse(predictions={1: 0.3}, hotkey='hk_alice')
        _run(handle_prediction(mock_validator, s1))

        # Immediate re-prediction hits cooldown
        s2 = make_synapse(predictions={1: 0.4}, hotkey='hk_alice')
        result = _run(handle_prediction(mock_validator, s2))
        assert result.accepted is False
        assert 'cooldown' in result.rejection_reason

    @patch('gittensor.validator.merge_predictions.handler.validate_prediction_values', return_value=None)
    @patch('gittensor.validator.merge_predictions.handler.validate_github_credentials', return_value=('gh_alice', None))
    @patch('gittensor.validator.merge_predictions.handler.check_prs_open', return_value=None)
    @patch('gittensor.validator.merge_predictions.handler.check_issue_active', return_value=None)
    def test_reject_total_exceeds_one(self, _cia, _cpo, _vgc, _vpv, mock_validator, make_synapse):
        from gittensor.validator.merge_predictions.handler import handle_prediction

        # Seed existing prediction via storage directly to avoid cooldown
        mock_validator.mp_storage.store_prediction(
            uid=0, hotkey='hk_alice', github_id='gh_alice', issue_id=1,
            repository='test/repo', pr_number=1, prediction=0.8, variance_at_prediction=0.0,
        )

        # New prediction on different PR would push total > 1.0
        s = make_synapse(predictions={2: 0.5}, hotkey='hk_alice')
        result = _run(handle_prediction(mock_validator, s))
        assert result.accepted is False
        assert 'exceed 1.0' in result.rejection_reason

    def test_blacklist_unregistered_hotkey(self, mock_validator, make_synapse):
        from gittensor.validator.merge_predictions.handler import blacklist_prediction

        synapse = make_synapse(hotkey='hk_unknown')
        is_blacklisted, reason = _run(blacklist_prediction(mock_validator, synapse))
        assert is_blacklisted is True
        assert 'Unregistered' in reason

    def test_blacklist_allows_registered(self, mock_validator, make_synapse):
        from gittensor.validator.merge_predictions.handler import blacklist_prediction

        synapse = make_synapse(hotkey='hk_alice')
        is_blacklisted, _ = _run(blacklist_prediction(mock_validator, synapse))
        assert is_blacklisted is False

    def test_priority_by_stake(self, mock_validator, make_synapse):
        from gittensor.validator.merge_predictions.handler import priority_prediction

        synapse = make_synapse(hotkey='hk_alice')
        priority = _run(priority_prediction(mock_validator, synapse))
        assert priority == pytest.approx(100.0)


# =============================================================================
# 3. Scoring
# =============================================================================


class TestPredictionScoring:
    """Pure function tests for scoring math."""

    # -- Correctness --

    def test_correctness_merged_pr(self):
        result = score_correctness(0.9, 1.0)
        assert result == pytest.approx(0.9 ** PREDICTIONS_CORRECTNESS_EXPONENT)

    def test_correctness_non_merged_pr(self):
        result = score_correctness(0.1, 0.0)
        assert result == pytest.approx(0.9 ** PREDICTIONS_CORRECTNESS_EXPONENT)

    def test_correctness_wrong_prediction(self):
        result = score_correctness(0.3, 1.0)
        assert result == pytest.approx(0.3 ** PREDICTIONS_CORRECTNESS_EXPONENT)

    def test_correctness_uniform_spray(self):
        result = score_correctness(0.25, 1.0)
        assert result == pytest.approx(0.25 ** PREDICTIONS_CORRECTNESS_EXPONENT)

    # -- Timeliness --

    def test_timeliness_at_pr_open(self, base_times):
        result = score_timeliness(
            base_times['pr_open'], base_times['settlement'], base_times['pr_open']
        )
        assert result == pytest.approx(PREDICTIONS_MAX_TIMELINESS_BONUS)

    def test_timeliness_at_settlement(self, base_times):
        result = score_timeliness(
            base_times['settlement'], base_times['settlement'], base_times['pr_open']
        )
        assert result == pytest.approx(0.0)

    def test_timeliness_midpoint(self, base_times):
        midpoint = base_times['pr_open'] + timedelta(days=15)
        result = score_timeliness(midpoint, base_times['settlement'], base_times['pr_open'])
        expected = PREDICTIONS_MAX_TIMELINESS_BONUS * (0.5 ** PREDICTIONS_TIMELINESS_EXPONENT)
        assert result == pytest.approx(expected)

    def test_timeliness_zero_window(self):
        t = datetime(2025, 6, 1, tzinfo=timezone.utc)
        assert score_timeliness(t, t, t) == 0.0

    # -- Consensus --

    def test_consensus_before_peak(self, base_times):
        result = score_consensus_bonus(
            base_times['prediction_early'], base_times['peak_variance'], base_times['settlement']
        )
        assert result == pytest.approx(PREDICTIONS_MAX_CONSENSUS_BONUS)

    def test_consensus_at_peak(self, base_times):
        result = score_consensus_bonus(
            base_times['peak_variance'], base_times['peak_variance'], base_times['settlement']
        )
        assert result == pytest.approx(PREDICTIONS_MAX_CONSENSUS_BONUS)

    def test_consensus_after_peak_midway(self, base_times):
        peak = base_times['peak_variance']
        settle = base_times['settlement']
        mid = peak + (settle - peak) / 2
        result = score_consensus_bonus(mid, peak, settle)
        assert result == pytest.approx(PREDICTIONS_MAX_CONSENSUS_BONUS * 0.5)

    def test_consensus_at_settlement(self, base_times):
        result = score_consensus_bonus(
            base_times['settlement'], base_times['peak_variance'], base_times['settlement']
        )
        assert result == pytest.approx(0.0)

    # -- Order --

    def test_order_rank_1(self):
        assert score_order_bonus(1) == pytest.approx(PREDICTIONS_MAX_ORDER_BONUS)

    def test_order_rank_2(self):
        assert score_order_bonus(2) == pytest.approx(PREDICTIONS_MAX_ORDER_BONUS / 2)

    def test_order_rank_0_unqualified(self):
        assert score_order_bonus(0) == 0.0

    def test_compute_order_ranks_filters_below_threshold(self):
        preds = {
            0: [PrPrediction(pr_number=1, prediction=0.5, prediction_time=datetime(2025, 6, 1, tzinfo=timezone.utc), variance_at_prediction=0.0)],
            1: [PrPrediction(pr_number=1, prediction=0.9, prediction_time=datetime(2025, 6, 2, tzinfo=timezone.utc), variance_at_prediction=0.0)],
        }
        ranks = compute_merged_pr_order_ranks(preds, merged_pr_number=1)
        assert 0 not in ranks
        assert ranks[1] == 1

    def test_compute_order_ranks_sorts_by_time(self):
        t1 = datetime(2025, 6, 1, tzinfo=timezone.utc)
        t2 = datetime(2025, 6, 2, tzinfo=timezone.utc)
        preds = {
            0: [PrPrediction(pr_number=1, prediction=0.9, prediction_time=t2, variance_at_prediction=0.0)],
            1: [PrPrediction(pr_number=1, prediction=0.8, prediction_time=t1, variance_at_prediction=0.0)],
        }
        ranks = compute_merged_pr_order_ranks(preds, merged_pr_number=1)
        assert ranks[1] == 1  # earlier
        assert ranks[0] == 2

    # -- Aggregation: score_miner_issue --

    def test_score_miner_issue_weighted_mean(self, base_times, sample_outcomes):
        """Merged PR gets weight=N in the issue score (N = total PRs)."""
        t = base_times['prediction_early']
        preds = [
            PrPrediction(pr_number=1, prediction=0.9, prediction_time=t, variance_at_prediction=0.05),
            PrPrediction(pr_number=2, prediction=0.05, prediction_time=t, variance_at_prediction=0.05),
            PrPrediction(pr_number=3, prediction=0.03, prediction_time=t, variance_at_prediction=0.05),
            PrPrediction(pr_number=4, prediction=0.02, prediction_time=t, variance_at_prediction=0.05),
        ]
        result = score_miner_issue(
            uid=0,
            predictions=preds,
            outcomes=sample_outcomes,
            settlement_time=base_times['settlement'],
            peak_variance_time=base_times['peak_variance'],
            merged_pr_order_ranks={0: 1},
        )
        assert isinstance(result, MinerIssueScore)
        assert result.issue_score > 0
        merged_score = next(ps for ps in result.pr_scores if ps.pr_number == 1)
        assert merged_score.score > 0

    # -- EMA --

    def test_update_ema(self):
        result = update_ema(current_round_score=1.0, previous_ema=0.0)
        expected = PREDICTIONS_EMA_BETA * 1.0 + (1.0 - PREDICTIONS_EMA_BETA) * 0.0
        assert result == pytest.approx(expected)


# =============================================================================
# 4. Validation
# =============================================================================


class TestValidation:
    """Pure function tests for validate_prediction_values."""

    def test_valid_predictions(self):
        assert validate_prediction_values({1: 0.5, 2: 0.3}) is None

    def test_empty_predictions(self):
        result = validate_prediction_values({})
        assert result is not None
        assert 'Empty' in result

    def test_negative_pr_number(self):
        result = validate_prediction_values({-1: 0.5})
        assert result is not None
        assert 'Invalid PR number' in result

    def test_value_out_of_range(self):
        result = validate_prediction_values({1: 1.5})
        assert result is not None
        assert 'out of range' in result

    def test_total_exceeds_one(self):
        result = validate_prediction_values({1: 0.6, 2: 0.5})
        assert result is not None
        assert 'exceeds 1.0' in result


# =============================================================================
# 5. Settlement
# =============================================================================


class TestSettlement:
    """Tests for merge_predictions() settlement orchestrator.

    Settlement now queries COMPLETED and CANCELLED issues from the contract
    (not ACTIVE). Predictions are deleted after settlement as the "settled" marker.
    """

    def _seed_predictions(self, mp_storage, uid, hotkey, github_id, issue_id, preds):
        """Helper: store a set of predictions for a miner."""
        for pr_num, value in preds.items():
            mp_storage.store_prediction(
                uid=uid, hotkey=hotkey, github_id=github_id, issue_id=issue_id,
                repository='test/repo', pr_number=pr_num, prediction=value, variance_at_prediction=0.05,
            )

    def _make_contract_issue(self, issue_id=1, repo='test/repo', issue_number=10):
        issue = MagicMock()
        issue.id = issue_id
        issue.repository_full_name = repo
        issue.issue_number = issue_number
        return issue

    def _setup_contract_mock(self, MockContract, completed=None, cancelled=None):
        """Configure the contract mock to return different issues per status."""
        from gittensor.validator.issue_competitions.contract_client import IssueStatus

        def get_issues_side_effect(status):
            if status == IssueStatus.COMPLETED:
                return completed or []
            elif status == IssueStatus.CANCELLED:
                return cancelled or []
            return []

        MockContract.return_value.get_issues_by_status.side_effect = get_issues_side_effect

    @patch('gittensor.validator.merge_predictions.settlement.get_pr_open_times')
    @patch('gittensor.validator.merge_predictions.settlement.check_github_issue_closed')
    @patch('gittensor.validator.merge_predictions.settlement.get_contract_address', return_value='5Faddr')
    @patch('gittensor.validator.merge_predictions.settlement.GITTENSOR_VALIDATOR_PAT', 'ghp_test')
    @patch('gittensor.validator.merge_predictions.settlement.IssueCompetitionContractClient')
    def test_settle_completed_issue_updates_ema(
        self, MockContract, _gca, mock_check_closed, mock_pr_times, mock_validator
    ):
        from gittensor.validator.merge_predictions.settlement import merge_predictions

        pr_open_time = datetime(2025, 6, 1, tzinfo=timezone.utc)

        contract_issue = self._make_contract_issue()
        self._setup_contract_mock(MockContract, completed=[contract_issue])

        mock_check_closed.return_value = {'is_closed': True, 'pr_number': 1}
        mock_pr_times.return_value = {1: pr_open_time, 2: pr_open_time}

        self._seed_predictions(mock_validator.mp_storage, uid=0, hotkey='hk_alice', github_id='gh_alice', issue_id=1, preds={1: 0.7, 2: 0.2})

        _run(merge_predictions(mock_validator, {}))

        ema = mock_validator.mp_storage.get_ema('gh_alice')
        assert ema > 0
        # Predictions deleted after settlement
        assert mock_validator.mp_storage.get_predictions_for_issue(1) == []

    @patch('gittensor.validator.merge_predictions.settlement.get_pr_open_times')
    @patch('gittensor.validator.merge_predictions.settlement.check_github_issue_closed')
    @patch('gittensor.validator.merge_predictions.settlement.get_contract_address', return_value='5Faddr')
    @patch('gittensor.validator.merge_predictions.settlement.GITTENSOR_VALIDATOR_PAT', 'ghp_test')
    @patch('gittensor.validator.merge_predictions.settlement.IssueCompetitionContractClient')
    def test_settle_multiple_completed_issues(
        self, MockContract, _gca, mock_check_closed, mock_pr_times, mock_validator
    ):
        from gittensor.validator.merge_predictions.settlement import merge_predictions

        pr_open_time = datetime(2025, 6, 1, tzinfo=timezone.utc)

        issue1 = self._make_contract_issue(issue_id=1, issue_number=10)
        issue2 = self._make_contract_issue(issue_id=2, issue_number=20)
        self._setup_contract_mock(MockContract, completed=[issue1, issue2])

        mock_check_closed.return_value = {'is_closed': True, 'pr_number': 1}
        mock_pr_times.return_value = {1: pr_open_time}

        self._seed_predictions(mock_validator.mp_storage, uid=0, hotkey='hk_alice', github_id='gh_alice', issue_id=1, preds={1: 0.8})
        self._seed_predictions(mock_validator.mp_storage, uid=0, hotkey='hk_alice', github_id='gh_alice', issue_id=2, preds={1: 0.9})

        _run(merge_predictions(mock_validator, {}))

        emas = mock_validator.mp_storage.get_all_emas()
        gh_alice = next(e for e in emas if e['github_id'] == 'gh_alice')
        assert gh_alice['rounds'] == 2
        # Both issues' predictions deleted
        assert mock_validator.mp_storage.get_predictions_for_issue(1) == []
        assert mock_validator.mp_storage.get_predictions_for_issue(2) == []

    @patch('gittensor.validator.merge_predictions.settlement.check_github_issue_closed')
    @patch('gittensor.validator.merge_predictions.settlement.get_contract_address', return_value='5Faddr')
    @patch('gittensor.validator.merge_predictions.settlement.GITTENSOR_VALIDATOR_PAT', 'ghp_test')
    @patch('gittensor.validator.merge_predictions.settlement.IssueCompetitionContractClient')
    def test_cancelled_issue_no_merge_no_ema_impact(
        self, MockContract, _gca, mock_check_closed, mock_validator
    ):
        """Cancelled issue with no merged PR: predictions voided, no EMA impact."""
        from gittensor.validator.merge_predictions.settlement import merge_predictions

        contract_issue = self._make_contract_issue()
        self._setup_contract_mock(MockContract, cancelled=[contract_issue])

        mock_check_closed.return_value = {'is_closed': True, 'pr_number': None}

        self._seed_predictions(mock_validator.mp_storage, uid=0, hotkey='hk_alice', github_id='gh_alice', issue_id=1, preds={1: 0.8})

        _run(merge_predictions(mock_validator, {}))

        assert mock_validator.mp_storage.get_ema('gh_alice') == 0.0
        # Predictions deleted even though voided
        assert mock_validator.mp_storage.get_predictions_for_issue(1) == []

    @patch('gittensor.validator.merge_predictions.settlement.get_pr_open_times')
    @patch('gittensor.validator.merge_predictions.settlement.check_github_issue_closed')
    @patch('gittensor.validator.merge_predictions.settlement.get_contract_address', return_value='5Faddr')
    @patch('gittensor.validator.merge_predictions.settlement.GITTENSOR_VALIDATOR_PAT', 'ghp_test')
    @patch('gittensor.validator.merge_predictions.settlement.IssueCompetitionContractClient')
    def test_cancelled_issue_with_merge_still_scored(
        self, MockContract, _gca, mock_check_closed, mock_pr_times, mock_validator
    ):
        """Cancelled but PR was merged (solver not in subnet): predictions still scored."""
        from gittensor.validator.merge_predictions.settlement import merge_predictions

        pr_open_time = datetime(2025, 6, 1, tzinfo=timezone.utc)

        contract_issue = self._make_contract_issue()
        self._setup_contract_mock(MockContract, cancelled=[contract_issue])

        mock_check_closed.return_value = {'is_closed': True, 'pr_number': 1}
        mock_pr_times.return_value = {1: pr_open_time, 2: pr_open_time}

        self._seed_predictions(mock_validator.mp_storage, uid=0, hotkey='hk_alice', github_id='gh_alice', issue_id=1, preds={1: 0.7, 2: 0.2})

        _run(merge_predictions(mock_validator, {}))

        ema = mock_validator.mp_storage.get_ema('gh_alice')
        assert ema > 0
        # Predictions deleted after scoring
        assert mock_validator.mp_storage.get_predictions_for_issue(1) == []

    @patch('gittensor.validator.merge_predictions.settlement.get_contract_address', return_value='5Faddr')
    @patch('gittensor.validator.merge_predictions.settlement.GITTENSOR_VALIDATOR_PAT', 'ghp_test')
    @patch('gittensor.validator.merge_predictions.settlement.IssueCompetitionContractClient')
    def test_already_settled_skipped(
        self, MockContract, _gca, mock_validator
    ):
        """Already-settled issues are skipped without calling GitHub, even if predictions exist."""
        from gittensor.validator.merge_predictions.settlement import merge_predictions

        contract_issue = self._make_contract_issue()
        self._setup_contract_mock(MockContract, completed=[contract_issue])

        # Pre-mark as settled and seed predictions anyway
        mock_validator.mp_storage.mark_issue_settled(contract_issue.id, 'scored', merged_pr_number=1)
        self._seed_predictions(mock_validator.mp_storage, uid=0, hotkey='hk_alice', github_id='gh_alice', issue_id=1, preds={1: 0.8})

        with patch('gittensor.validator.merge_predictions.settlement.check_github_issue_closed') as mock_check:
            _run(merge_predictions(mock_validator, {}))
            # GitHub should NOT be called since issue is already settled
            mock_check.assert_not_called()

        # Predictions should be untouched (not deleted by settlement)
        assert len(mock_validator.mp_storage.get_predictions_for_issue(1)) == 1

    @patch('gittensor.validator.merge_predictions.settlement.get_pr_open_times')
    @patch('gittensor.validator.merge_predictions.settlement.check_github_issue_closed')
    @patch('gittensor.validator.merge_predictions.settlement.get_contract_address', return_value='5Faddr')
    @patch('gittensor.validator.merge_predictions.settlement.GITTENSOR_VALIDATOR_PAT', 'ghp_test')
    @patch('gittensor.validator.merge_predictions.settlement.IssueCompetitionContractClient')
    def test_deregistered_miner_skipped(
        self, MockContract, _gca, mock_check_closed, mock_pr_times, mock_validator
    ):
        from gittensor.validator.merge_predictions.settlement import merge_predictions

        pr_open_time = datetime(2025, 6, 1, tzinfo=timezone.utc)
        contract_issue = self._make_contract_issue()
        self._setup_contract_mock(MockContract, completed=[contract_issue])

        mock_check_closed.return_value = {'is_closed': True, 'pr_number': 1}
        mock_pr_times.return_value = {1: pr_open_time}

        self._seed_predictions(mock_validator.mp_storage, uid=0, hotkey='hk_alice', github_id='gh_alice', issue_id=1, preds={1: 0.7})
        self._seed_predictions(mock_validator.mp_storage, uid=5, hotkey='hk_gone', github_id='gh_gone', issue_id=1, preds={1: 0.6})

        _run(merge_predictions(mock_validator, {}))

        assert mock_validator.mp_storage.get_ema('gh_alice') > 0
        assert mock_validator.mp_storage.get_ema('gh_gone') == 0.0
        # Predictions deleted for all miners (including deregistered)
        assert mock_validator.mp_storage.get_predictions_for_issue(1) == []

    @patch('gittensor.validator.merge_predictions.settlement.get_pr_open_times')
    @patch('gittensor.validator.merge_predictions.settlement.check_github_issue_closed')
    @patch('gittensor.validator.merge_predictions.settlement.get_contract_address', return_value='5Faddr')
    @patch('gittensor.validator.merge_predictions.settlement.GITTENSOR_VALIDATOR_PAT', 'ghp_test')
    @patch('gittensor.validator.merge_predictions.settlement.IssueCompetitionContractClient')
    def test_ema_persists_across_settlements(
        self, MockContract, _gca, mock_check_closed, mock_pr_times, mock_validator
    ):
        from gittensor.validator.merge_predictions.settlement import merge_predictions

        pr_open_time = datetime(2025, 6, 1, tzinfo=timezone.utc)

        # First settlement
        issue1 = self._make_contract_issue(issue_id=1, issue_number=10)
        self._setup_contract_mock(MockContract, completed=[issue1])
        mock_check_closed.return_value = {'is_closed': True, 'pr_number': 1}
        mock_pr_times.return_value = {1: pr_open_time}

        self._seed_predictions(mock_validator.mp_storage, uid=0, hotkey='hk_alice', github_id='gh_alice', issue_id=1, preds={1: 0.8})

        _run(merge_predictions(mock_validator, {}))
        ema_after_first = mock_validator.mp_storage.get_ema('gh_alice')
        assert ema_after_first > 0
        assert mock_validator.mp_storage.get_predictions_for_issue(1) == []

        # Second settlement with a new issue
        issue2 = self._make_contract_issue(issue_id=2, issue_number=20)
        self._setup_contract_mock(MockContract, completed=[issue2])
        mock_pr_times.return_value = {1: pr_open_time}

        self._seed_predictions(mock_validator.mp_storage, uid=0, hotkey='hk_alice', github_id='gh_alice', issue_id=2, preds={1: 0.9})

        _run(merge_predictions(mock_validator, {}))
        ema_after_second = mock_validator.mp_storage.get_ema('gh_alice')

        assert ema_after_second != ema_after_first

    @patch('gittensor.validator.merge_predictions.settlement.get_pr_open_times')
    @patch('gittensor.validator.merge_predictions.settlement.check_github_issue_closed')
    @patch('gittensor.validator.merge_predictions.settlement.get_contract_address', return_value='5Faddr')
    @patch('gittensor.validator.merge_predictions.settlement.GITTENSOR_VALIDATOR_PAT', 'ghp_test')
    @patch('gittensor.validator.merge_predictions.settlement.IssueCompetitionContractClient')
    def test_settled_issue_recorded_after_scoring(
        self, MockContract, _gca, mock_check_closed, mock_pr_times, mock_validator
    ):
        """After completed settlement, issue is recorded in settled_issues."""
        from gittensor.validator.merge_predictions.settlement import merge_predictions

        pr_open_time = datetime(2025, 6, 1, tzinfo=timezone.utc)
        contract_issue = self._make_contract_issue()
        self._setup_contract_mock(MockContract, completed=[contract_issue])

        mock_check_closed.return_value = {'is_closed': True, 'pr_number': 1}
        mock_pr_times.return_value = {1: pr_open_time}

        self._seed_predictions(mock_validator.mp_storage, uid=0, hotkey='hk_alice', github_id='gh_alice', issue_id=1, preds={1: 0.8})

        _run(merge_predictions(mock_validator, {}))

        assert mock_validator.mp_storage.is_issue_settled(1) is True

    @patch('gittensor.validator.merge_predictions.settlement.check_github_issue_closed')
    @patch('gittensor.validator.merge_predictions.settlement.get_contract_address', return_value='5Faddr')
    @patch('gittensor.validator.merge_predictions.settlement.GITTENSOR_VALIDATOR_PAT', 'ghp_test')
    @patch('gittensor.validator.merge_predictions.settlement.IssueCompetitionContractClient')
    def test_voided_issue_recorded(
        self, MockContract, _gca, mock_check_closed, mock_validator
    ):
        """After voiding a cancelled issue, it is recorded in settled_issues."""
        from gittensor.validator.merge_predictions.settlement import merge_predictions

        contract_issue = self._make_contract_issue()
        self._setup_contract_mock(MockContract, cancelled=[contract_issue])

        mock_check_closed.return_value = {'is_closed': True, 'pr_number': None}

        self._seed_predictions(mock_validator.mp_storage, uid=0, hotkey='hk_alice', github_id='gh_alice', issue_id=1, preds={1: 0.8})

        _run(merge_predictions(mock_validator, {}))

        assert mock_validator.mp_storage.is_issue_settled(1) is True

    @patch('gittensor.validator.merge_predictions.settlement.GITTENSOR_VALIDATOR_PAT', '')
    def test_no_validator_pat_skips(self, mock_validator):
        from gittensor.validator.merge_predictions.settlement import merge_predictions

        _run(merge_predictions(mock_validator, {}))

        assert mock_validator.mp_storage.get_all_emas() == []
