# Entrius 2025

from datetime import datetime, timezone
from typing import cast

from gittensor.classes import FileChange, Issue, MinerEvaluation, MinerEvaluationCache, PRState, PullRequest
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


class TestCacheIsolation:
    """Lock down the invariants required by the non-deepcopy copy strategy:
    caller mutations must not leak into the cache, and heavy file_changes
    must not be retained on cached PRs."""

    def _eval_with_issue(self) -> MinerEvaluation:
        now = datetime.now(timezone.utc)
        pr = PullRequest(
            number=7,
            repository_full_name='owner/repo',
            uid=1,
            hotkey='hotkey_1',
            github_id='12345',
            title='pr',
            author_login='miner',
            merged_at=now,
            created_at=now,
            pr_state=PRState.MERGED,
            file_changes=[
                FileChange(
                    pr_number=7,
                    repository_full_name='owner/repo',
                    filename='a.py',
                    changes=1,
                    additions=1,
                    deletions=0,
                    status='added',
                    patch='heavy patch contents',
                )
            ],
            issues=[
                Issue(
                    number=42,
                    pr_number=7,
                    repository_full_name='owner/repo',
                    title='issue',
                    author_github_id='99',
                )
            ],
        )
        ev = MinerEvaluation(uid=1, hotkey='hotkey_1', github_id='12345', github_pat='secret')
        ev.merged_pull_requests = [pr]
        return ev

    def test_cache_drops_file_changes_and_pat(self):
        cache = MinerEvaluationCache()
        source = self._eval_with_issue()

        cache.store(source)

        # Source must be untouched — downstream DB storage still needs patches.
        assert source.github_pat == 'secret'
        source_file_changes = source.merged_pull_requests[0].file_changes
        assert source_file_changes is not None
        assert source_file_changes[0].patch == 'heavy patch contents'

        cached = cache.get(uid=1, hotkey='hotkey_1', github_id='12345')
        assert cached is not None
        assert cached.github_pat is None
        assert cached.merged_pull_requests[0].file_changes is None

    def test_get_returns_isolated_issues(self):
        cache = MinerEvaluationCache()
        cache.store(self._eval_with_issue())

        first = cache.get(uid=1, hotkey='hotkey_1', github_id='12345')
        assert first is not None
        first_issues = first.merged_pull_requests[0].issues
        assert first_issues is not None
        first_issues[0].discovery_earned_score = 999.0
        first.issue_discovery_score = 123.0

        second = cache.get(uid=1, hotkey='hotkey_1', github_id='12345')
        assert second is not None
        second_issues = second.merged_pull_requests[0].issues
        assert second_issues is not None
        assert second_issues[0].discovery_earned_score == 0.0
        assert second.issue_discovery_score == 0.0

    def test_store_isolates_cache_from_source_issue_mutations(self):
        cache = MinerEvaluationCache()
        source = self._eval_with_issue()
        cache.store(source)

        # Simulate downstream scoring mutating the source eval's Issue.
        source_issues = source.merged_pull_requests[0].issues
        assert source_issues is not None
        source_issues[0].discovery_earned_score = 42.0

        cached = cache.get(uid=1, hotkey='hotkey_1', github_id='12345')
        assert cached is not None
        cached_issues = cached.merged_pull_requests[0].issues
        assert cached_issues is not None
        assert cached_issues[0].discovery_earned_score == 0.0
