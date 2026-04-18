# Entrius 2025

from datetime import datetime, timezone
from typing import cast

from gittensor.classes import MinerEvaluation, MinerEvaluationCache, PRState, PullRequest
from neurons.validator import Validator


class _DummyValidator:
    def __init__(self):
        self.evaluation_cache = MinerEvaluationCache()


def _make_pr(uid: int) -> PullRequest:
    now = datetime.now(timezone.utc)
    return PullRequest(
        number=1,
        repository_full_name='owner/repo',
        uid=uid,
        hotkey='hotkey_1',
        github_id='12345',
        title='cached pr',
        author_login='miner',
        merged_at=now,
        created_at=now,
        pr_state=PRState.MERGED,
        file_changes=[],
    )


def _build_eval(uid: int, merged_prs: int, fetch_failed: bool) -> MinerEvaluation:
    eval_ = MinerEvaluation(uid=uid, hotkey='hotkey_1', github_id='12345')
    eval_.merged_pull_requests = [_make_pr(uid) for _ in range(merged_prs)]
    eval_.github_pr_fetch_failed = fetch_failed
    return eval_


class TestStoreOrUseCachedEvaluation:
    def test_legitimate_zero_prs_does_not_use_cache(self):
        validator = _DummyValidator()
        validator.evaluation_cache.store(_build_eval(uid=1, merged_prs=1, fetch_failed=False))

        current_eval = _build_eval(uid=1, merged_prs=0, fetch_failed=False)
        miner_evaluations = {1: current_eval}

        cached_uids = Validator.store_or_use_cached_evaluation(cast(Validator, validator), miner_evaluations)

        assert cached_uids == set()
        assert miner_evaluations[1] is current_eval
        assert miner_evaluations[1].total_prs == 0

    def test_fetch_failure_with_zero_prs_uses_cache(self):
        validator = _DummyValidator()
        validator.evaluation_cache.store(_build_eval(uid=1, merged_prs=1, fetch_failed=False))

        current_eval = _build_eval(uid=1, merged_prs=0, fetch_failed=True)
        miner_evaluations = {1: current_eval}

        cached_uids = Validator.store_or_use_cached_evaluation(cast(Validator, validator), miner_evaluations)

        assert cached_uids == {1}
        assert miner_evaluations[1] is not current_eval
        assert miner_evaluations[1].total_prs == 1

    def test_fetch_failure_after_partial_load_skips_cache_store_and_fallback(self):
        validator = _DummyValidator()
        validator.evaluation_cache.store(_build_eval(uid=1, merged_prs=2, fetch_failed=False))

        current_eval = _build_eval(uid=1, merged_prs=1, fetch_failed=True)
        miner_evaluations = {1: current_eval}

        cached_uids = Validator.store_or_use_cached_evaluation(cast(Validator, validator), miner_evaluations)

        assert cached_uids == set()
        assert miner_evaluations[1] is current_eval
        assert miner_evaluations[1].total_prs == 1

        cached_eval = validator.evaluation_cache.get(uid=1, hotkey='hotkey_1', github_id='12345')
        assert cached_eval is not None
        assert cached_eval.total_prs == 2
