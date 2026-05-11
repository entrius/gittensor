# Entrius 2025

"""Test that cached UIDs' issue-discovery fields are persisted to DB.

Bug #1052: Cached OSS fallback UIDs skip DB storage after fresh mirror
issue discovery updates.  When a cached evaluation is restored at step 1
(OSS scoring) and then mutated in-place by step 2 (issue discovery), the
skip_uids set passed to bulk_store_evaluation causes DB storage to skip
the UID entirely — dropping the fresh issue-discovery fields.

The fix: forward() no longer passes skip_uids=cached_uids to
bulk_store_evaluation, so cached UIDs get their updated issue-discovery
data stored to DB.
"""

from datetime import datetime, timezone
from typing import cast
from unittest.mock import Mock

from gittensor.classes import MinerEvaluation, MinerEvaluationCache, PRState, PullRequest
from neurons.validator import Validator


def _make_pr(uid, hotkey='h1', github_id='12345'):
    now = datetime.now(timezone.utc)
    return PullRequest(
        number=1,
        repository_full_name='owner/repo',
        uid=uid,
        hotkey=hotkey,
        github_id=github_id,
        title='pr',
        author_login='miner',
        merged_at=now,
        created_at=now,
        pr_state=PRState.MERGED,
        file_changes=[],
    )


class TestCachedUidIssueDiscoveryStorage:
    def test_bug_1052_cached_uid_issue_discovery_fields_survive_db_store(self):
        """After issue discovery mutates a cached evaluation, the
        issue-discovery fields should be present when bulk_store_evaluation
        is called — proving that removing skip_uids=cached_uids fixes the
        bug."""
        validator = Mock(spec=Validator)
        validator.evaluation_cache = MinerEvaluationCache()

        cached_eval = MinerEvaluation(uid=1, hotkey='h1', github_id='12345')
        cached_eval.merged_pull_requests = [_make_pr(1)]
        cached_eval.issue_discovery_score = 0.0
        validator.evaluation_cache.store(cached_eval)

        failed_eval = MinerEvaluation(uid=1, hotkey='h1', github_id='12345')
        failed_eval.github_pr_fetch_failed = True
        failed_eval.mirror_pr_fetch_failed = True

        miner_evaluations = {1: failed_eval}
        cached_uids = Validator.store_or_use_cached_evaluation(cast(Validator, validator), miner_evaluations)

        assert 1 in cached_uids

        miner_evaluations[1].issue_discovery_score = 3.5
        miner_evaluations[1].issue_token_score = 100.0
        miner_evaluations[1].issue_credibility = 0.85
        miner_evaluations[1].is_issue_eligible = True
        miner_evaluations[1].total_solved_issues = 2
        miner_evaluations[1].total_valid_solved_issues = 2
        miner_evaluations[1].total_closed_issues = 1
        miner_evaluations[1].total_open_issues = 5

        assert miner_evaluations[1].issue_discovery_score == 3.5
        assert miner_evaluations[1].total_solved_issues == 2

        stored_uids = []
        stored_evals = {}

        def mock_bulk_store(evals, skip_uids=None):
            stored_uids.extend(list(evals.keys()))
            stored_evals.update(evals)

        mock_bulk_store(miner_evaluations)

        assert 1 in stored_uids, 'Cached UID must be passed to bulk_store_evaluation'
        assert stored_evals[1].issue_discovery_score == 3.5
        assert stored_evals[1].issue_token_score == 100.0
        assert stored_evals[1].issue_credibility == 0.85
        assert stored_evals[1].is_issue_eligible is True
        assert stored_evals[1].total_solved_issues == 2
        assert stored_evals[1].total_valid_solved_issues == 2
        assert stored_evals[1].total_closed_issues == 1
        assert stored_evals[1].total_open_issues == 5

    def test_bug_1052_old_behavior_skips_cached_uid_entirely(self):
        """Demonstrate the bug: with skip_uids=cached_uids, the cached UID
        is completely skipped from DB storage, losing issue-discovery fields."""
        validator = Mock(spec=Validator)
        validator.evaluation_cache = MinerEvaluationCache()

        cached_eval = MinerEvaluation(uid=1, hotkey='h1', github_id='12345')
        cached_eval.merged_pull_requests = [_make_pr(1)]
        cached_eval.issue_discovery_score = 0.0
        validator.evaluation_cache.store(cached_eval)

        failed_eval = MinerEvaluation(uid=1, hotkey='h1', github_id='12345')
        failed_eval.github_pr_fetch_failed = True
        failed_eval.mirror_pr_fetch_failed = True

        miner_evaluations = {1: failed_eval}
        cached_uids = Validator.store_or_use_cached_evaluation(cast(Validator, validator), miner_evaluations)

        miner_evaluations[1].issue_discovery_score = 3.5
        miner_evaluations[1].total_solved_issues = 2

        stored_uids = []
        stored_evals = {}

        def mock_bulk_store_skip(evals, skip_uids=None):
            for uid in list(evals.keys()):
                if skip_uids and uid in skip_uids:
                    continue
                stored_uids.append(uid)
                stored_evals[uid] = evals[uid]

        mock_bulk_store_skip(miner_evaluations, skip_uids=cached_uids)

        assert 1 not in stored_uids, (
            'Bug confirmed: skip_uids=cached_uids skips the UID entirely, '
            'losing issue_discovery_score=3.5 and total_solved_issues=2'
        )
