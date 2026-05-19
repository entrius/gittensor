from datetime import datetime, timedelta, timezone

from gittensor.classes import MinerEvaluation, MinerEvaluationCache


def _eval(uid: int = 1, hotkey: str = 'hk', github_id: str = '999') -> MinerEvaluation:
    return MinerEvaluation(uid=uid, hotkey=hotkey, github_id=github_id)


def _expire_cached_evaluation(cache: MinerEvaluationCache, uid: int) -> None:
    cache._cache[uid].cached_at = datetime.now(timezone.utc) - cache._max_age - timedelta(seconds=1)


def _expire_issue_discovery(cache: MinerEvaluationCache, uid: int) -> None:
    cache._cache[uid].issue_discovery_cached_at = datetime.now(timezone.utc) - cache._max_age - timedelta(seconds=1)


def test_get_returns_recent_cached_evaluation():
    cache = MinerEvaluationCache()
    evaluation = _eval()

    cache.store(evaluation)

    cached = cache.get(uid=1, hotkey='hk', github_id='999')

    assert cached is not None
    assert cached.uid == 1
    assert cached.hotkey == 'hk'
    assert cached.github_id == '999'


def test_get_evicts_expired_cached_evaluation():
    cache = MinerEvaluationCache()
    evaluation = _eval()
    cache.store(evaluation)
    _expire_cached_evaluation(cache, uid=1)

    cached = cache.get(uid=1, hotkey='hk', github_id='999')

    assert cached is None
    assert 1 not in cache._cache


def test_issue_discovery_cache_requires_issue_refresh_timestamp():
    cache = MinerEvaluationCache()
    evaluation = _eval()
    evaluation.issue_discovery_score = 8.12
    evaluation.total_solved_issues = 7

    cache.store(evaluation)

    assert cache.get(uid=1, hotkey='hk', github_id='999') is not None
    assert cache.get_issue_discovery(uid=1, hotkey='hk', github_id='999') is None


def test_issue_discovery_cache_returns_recent_refresh():
    cache = MinerEvaluationCache()
    evaluation = _eval()
    evaluation.issue_discovery_score = 8.12
    evaluation.total_solved_issues = 7

    cache.store(evaluation)
    cache.update_issue_discovery(evaluation)

    cached = cache.get_issue_discovery(uid=1, hotkey='hk', github_id='999')

    assert cached is not None
    assert cached.issue_discovery_score == 8.12
    assert cached.total_solved_issues == 7


def test_pr_cache_fallback_strips_expired_issue_discovery_fields():
    cache = MinerEvaluationCache()
    evaluation = _eval()
    evaluation.issue_discovery_score = 8.12
    evaluation.issue_token_score = 700.0
    evaluation.issue_credibility = 1.0
    evaluation.is_issue_eligible = True
    evaluation.total_solved_issues = 7
    evaluation.total_valid_solved_issues = 7
    cache.store(evaluation)
    cache.update_issue_discovery(evaluation)
    _expire_issue_discovery(cache, uid=1)

    cached = cache.get(uid=1, hotkey='hk', github_id='999')

    assert cached is not None
    assert cached.issue_discovery_score == 0.0
    assert cached.issue_token_score == 0.0
    assert cached.issue_credibility == 0.0
    assert cached.is_issue_eligible is False
    assert cached.total_solved_issues == 0
    assert cached.total_valid_solved_issues == 0
