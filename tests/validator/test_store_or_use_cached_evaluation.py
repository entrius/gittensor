"""Regression tests for Validator.store_or_use_cached_evaluation.

Issue #1323: a miner whose OSS fetch succeeded but yielded zero PRs (e.g. a
pure issue-discovery miner, or one whose PRs were all maintainer-filtered)
must still get a cache entry, so a subsequent same- or next-round mirror
failure during issue discovery can restore their prior issue-discovery
scores. Before the fix, the cache write was gated on ``total_prs > 0`` and
the entry never existed, leaving the miner scored zero on transient
failures while PR+issue miners were protected.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

classes = pytest.importorskip('gittensor.classes')
validator_module = pytest.importorskip('neurons.validator')

MinerEvaluation = classes.MinerEvaluation
MinerEvaluationCache = classes.MinerEvaluationCache
Validator = validator_module.Validator


def _call(cache, miner_evaluations):
    """Invoke the method without instantiating the full Validator class."""
    self_obj = SimpleNamespace(evaluation_cache=cache)
    return Validator.store_or_use_cached_evaluation(self_obj, miner_evaluations)


def _issue_only_miner(uid=1, hotkey='hk1', github_id='gh1'):
    miner = MinerEvaluation(uid=uid, hotkey=hotkey, github_id=github_id)
    miner.issue_discovery_score = 12.5
    miner.issue_token_score = 700.0
    miner.issue_credibility = 1.0
    miner.is_issue_eligible = True
    miner.total_solved_issues = 3
    miner.total_valid_solved_issues = 3
    return miner


class TestZeroPrCaching:
    def test_zero_pr_miner_is_cached_after_successful_oss_fetch(self):
        """The fix: total_prs == 0 must not gate the cache store."""
        cache = MinerEvaluationCache()
        miner = _issue_only_miner()
        assert miner.total_prs == 0

        _call(cache, {1: miner})

        cached = cache.get(uid=1, hotkey='hk1', github_id='gh1')
        assert cached is not None, 'issue-only miner must get a cache anchor'

    def test_update_issue_discovery_now_refreshes_zero_pr_entry(self):
        """End-to-end: zero-PR miner gets store()'d, then issue-discovery
        refresh sticks (previously the refresh no-op'd because no entry
        existed)."""
        cache = MinerEvaluationCache()
        miner = MinerEvaluation(uid=1, hotkey='hk1', github_id='gh1')

        _call(cache, {1: miner})

        miner.issue_discovery_score = 9.0
        miner.total_solved_issues = 4
        miner.total_valid_solved_issues = 4
        cache.update_issue_discovery(miner)

        cached = cache.get(uid=1, hotkey='hk1', github_id='gh1')
        assert cached is not None
        assert cached.issue_discovery_score == 9.0
        assert cached.total_solved_issues == 4
        assert cached.total_valid_solved_issues == 4


class TestOssFallbackGuard:
    def test_oss_failure_does_not_restore_issue_only_entry_as_pr_state(self):
        """A cached issue-only entry must not masquerade as authoritative PR
        state when the next round's OSS fetch fails — otherwise the miner
        would appear to have zero PRs as a 'restored' result rather than as
        a transient-failure outcome that propagates correctly downstream."""
        cache = MinerEvaluationCache()
        prior = _issue_only_miner(uid=1)
        cache.store(prior)
        assert cache.get(uid=1, hotkey='hk1', github_id='gh1') is not None

        # Round N: OSS fetch fails for this miner.
        failed = MinerEvaluation(uid=1, hotkey='hk1', github_id='gh1')
        failed.github_pr_fetch_failed = True
        failed.mirror_pr_fetch_failed = True

        miner_evaluations = {1: failed}
        cached_uids = _call(cache, miner_evaluations)

        # The entry exists but has no PR data — the fallback must skip it,
        # not swap it in. The original failed miner stays in the dict.
        assert 1 not in cached_uids
        assert miner_evaluations[1] is failed

    def test_oss_failure_still_restores_entry_with_pr_data(self):
        """Sanity: the existing fallback for PR-bearing miners must keep
        working — the new guard only filters out zero-PR entries."""
        from gittensor.validator.oss_contributions.mirror.scored_pr import ScoredPR
        from gittensor.utils.mirror.models import MirrorPullRequest

        cache = MinerEvaluationCache()
        with_prs = MinerEvaluation(uid=2, hotkey='hk2', github_id='gh2')
        pr = MirrorPullRequest.from_dict(
            {
                'repo_full_name': 'entrius/gittensor-ui',
                'pr_number': 1,
                'title': 't',
                'body': 'b',
                'state': 'MERGED',
                'author_github_id': '1',
                'author_login': 'a',
                'author_association': 'CONTRIBUTOR',
                'created_at': '2026-04-10T00:00:00Z',
                'closed_at': '2026-04-18T10:00:00Z',
                'merged_at': '2026-04-18T10:00:00Z',
                'last_edited_at': None,
                'edited_after_merge': False,
                'hours_since_merge': 1.0,
                'merged_by_login': 'm',
                'base_ref': 'test',
                'head_sha': 'h',
                'base_sha': 'b',
                'merge_base_sha': 'mb',
                'additions': 1,
                'deletions': 0,
                'commits_count': 1,
                'scoring_data_stored': True,
                'review_summary': {'maintainer_changes_requested_count': 0},
                'labels': [],
                'linked_issues': [],
            }
        )
        with_prs.merged_prs = [ScoredPR(pr=pr)]
        cache.store(with_prs)

        failed = MinerEvaluation(uid=2, hotkey='hk2', github_id='gh2')
        failed.github_pr_fetch_failed = True
        failed.mirror_pr_fetch_failed = True

        miner_evaluations = {2: failed}
        cached_uids = _call(cache, miner_evaluations)

        assert 2 in cached_uids
        assert miner_evaluations[2] is not failed
        assert miner_evaluations[2].total_prs == 1
