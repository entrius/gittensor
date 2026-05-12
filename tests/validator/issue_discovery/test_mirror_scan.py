"""Unit tests for run_mirror_issue_discovery.

Focus: anti-gaming gates fire correctly, bucketing between solved / closed /
ignored, and the per-miner MinerEvaluation issue fields get populated.
"""

from __future__ import annotations

import asyncio
from typing import Optional
from unittest.mock import Mock

import pytest

mirror_scan_module = pytest.importorskip(
    'gittensor.validator.issue_discovery.mirror_scan',
    reason='Requires gittensor mirror subpackage',
)
mirror_models = pytest.importorskip('gittensor.utils.mirror.models')
mirror_client_mod = pytest.importorskip('gittensor.utils.mirror.client')
classes = pytest.importorskip('gittensor.classes')
load_weights = pytest.importorskip('gittensor.validator.utils.load_weights')
scored_pr_module = pytest.importorskip('gittensor.validator.oss_contributions.mirror.scored_pr')
normalize_module = pytest.importorskip('gittensor.validator.issue_discovery.normalize')

run_mirror_issue_discovery = mirror_scan_module.run_mirror_issue_discovery
_classify_issue = mirror_scan_module._classify_issue
_build_solving_pr_cache = mirror_scan_module._build_solving_pr_cache
CachedSolvingPR = mirror_scan_module.CachedSolvingPR
MirrorIssue = mirror_models.MirrorIssue
MirrorIssuesResponse = mirror_models.MirrorIssuesResponse
MirrorPullRequest = mirror_models.MirrorPullRequest
MirrorPullRequestFilesResponse = mirror_models.MirrorPullRequestFilesResponse
MirrorRequestError = mirror_client_mod.MirrorRequestError
MinerEvaluation = classes.MinerEvaluation
MinerEvaluationCache = classes.MinerEvaluationCache
RepositoryConfig = load_weights.RepositoryConfig
TokenConfig = load_weights.TokenConfig
ScoredMirrorPR = scored_pr_module.ScoredMirrorPR
normalize_issue_discovery_rewards = normalize_module.normalize_issue_discovery_rewards


# Representative defaults for the plumbed-through token scoring args. The
# tests below populate the cache directly for cache-hit paths, so these are
# only used on cache-miss fetches (and even then MirrorPullRequestFilesResponse
# is mocked so no real token scoring math runs).
_EMPTY_LANGS = {}
_EMPTY_TOKEN_CONFIG = TokenConfig()


def _scored_mirror_pr(
    repo: str, pr_number: int, token_score: float = 100.0, base_score: float = 42.0
) -> ScoredMirrorPR:
    """Build a ScoredMirrorPR for cache pre-population in tests."""
    pr = MirrorPullRequest.from_dict(
        {
            'repo_full_name': repo,
            'pr_number': pr_number,
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
    scored = ScoredMirrorPR(pr=pr)
    scored.token_score = token_score
    scored.base_score = base_score
    return scored


def _empty_files_response(repo: str, pr_number: int) -> MirrorPullRequestFilesResponse:
    return MirrorPullRequestFilesResponse.from_dict(
        {
            'repo_full_name': repo,
            'pr_number': pr_number,
            'head_sha': 'h',
            'base_sha': 'b',
            'merge_base_sha': 'mb',
            'scoring_data_stored': True,
            'files': [],
        }
    )


def _issue_dict(
    issue_number: int = 50,
    state: str = 'CLOSED',
    state_reason: Optional[str] = 'COMPLETED',
    author_github_id: Optional[str] = '999',
    is_transferred: bool = False,
    solved_by_pr: Optional[int] = 100,
    solving_pr_state: str = 'MERGED',
    solving_pr_author: str = '218712309',
    solving_pr_edited_after_merge: bool = False,
    last_edited_at: Optional[str] = None,
    repo: str = 'entrius/gittensor-ui',
    created_at: str = '2026-04-01T00:00:00Z',
) -> dict:
    sp = None
    if solved_by_pr:
        sp = {
            'pr_number': solved_by_pr,
            'author_github_id': solving_pr_author,
            'state': solving_pr_state,
            'merged_at': '2026-04-18T10:00:00Z' if solving_pr_state == 'MERGED' else None,
            'hours_since_merge': 1.0,
            'edited_after_merge': solving_pr_edited_after_merge,
            'head_sha': 'h',
            'base_sha': 'b',
            'merge_base_sha': 'mb',
            'labels': [],
            'review_summary': {'maintainer_changes_requested_count': 0},
        }
    return {
        'repo_full_name': repo,
        'issue_number': issue_number,
        'title': 'test issue',
        'state': state,
        'state_reason': state_reason,
        'author_github_id': author_github_id,
        'author_login': 'discoverer',
        'author_association': 'CONTRIBUTOR',
        'created_at': created_at,
        'closed_at': '2026-04-18T10:00:00Z' if state == 'CLOSED' else None,
        'updated_at': '2026-04-18T10:00:00Z',
        'last_edited_at': last_edited_at,
        'is_transferred': is_transferred,
        'solved_by_pr': solved_by_pr,
        'labels': [],
        'solving_pr': sp,
    }


def _response(issue_dicts: list) -> MirrorIssuesResponse:
    return MirrorIssuesResponse.from_dict(
        {
            'github_id': '999',
            'since': '2026-03-15T00:00:00Z',
            'generated_at': '2026-04-21T00:00:00Z',
            'issues': issue_dicts,
        }
    )


def _eval(uid: int = 1, github_id: Optional[str] = '999'):
    return MinerEvaluation(uid=uid, hotkey='hk', github_id=github_id)


def _mirror_repos(*names: str) -> dict:
    return {name: RepositoryConfig(weight=0.5, mirror_enabled=True) for name in names}


def _run(coro):
    return asyncio.run(coro)


# ============================================================================
# _classify_issue (anti-gaming gates)
# ============================================================================


class TestClassifyIssue:
    def test_clean_completed_merged_is_solved(self):
        issue = MirrorIssue.from_dict(_issue_dict())
        assert _classify_issue(issue) == 'solved'

    def test_transferred_ignored(self):
        issue = MirrorIssue.from_dict(_issue_dict(is_transferred=True))
        assert _classify_issue(issue) == 'ignore'

    def test_open_issue_ignored(self):
        issue = MirrorIssue.from_dict(_issue_dict(state='OPEN', state_reason=None, solved_by_pr=None))
        assert _classify_issue(issue) == 'ignore'

    def test_not_planned_counts_as_closed(self):
        issue = MirrorIssue.from_dict(_issue_dict(state_reason='NOT_PLANNED'))
        assert _classify_issue(issue) == 'not-solved-closed'

    def test_null_state_reason_counts_as_closed(self):
        issue = MirrorIssue.from_dict(_issue_dict(state_reason=None, solved_by_pr=None))
        assert _classify_issue(issue) == 'not-solved-closed'

    def test_no_solving_pr_counts_as_closed(self):
        issue = MirrorIssue.from_dict(_issue_dict(solved_by_pr=None))
        assert _classify_issue(issue) == 'not-solved-closed'

    def test_solving_pr_not_merged_counts_as_closed(self):
        issue = MirrorIssue.from_dict(_issue_dict(solving_pr_state='OPEN'))
        assert _classify_issue(issue) == 'not-solved-closed'

    def test_solving_pr_edited_after_merge_counts_as_closed(self):
        issue = MirrorIssue.from_dict(_issue_dict(solving_pr_edited_after_merge=True))
        assert _classify_issue(issue) == 'not-solved-closed'

    def test_issue_edited_after_solving_pr_merge_counts_as_closed(self):
        # Anti-spec-rewrite: miner can't author a vague issue, then rewrite the
        # body after a third party's PR merges to retroactively claim discovery
        # credit for a fix they didn't anticipate.
        issue = MirrorIssue.from_dict(_issue_dict(last_edited_at='2026-04-18T10:00:01Z'))
        assert _classify_issue(issue) == 'not-solved-closed'

    def test_issue_edited_before_solving_pr_merge_is_solved(self):
        # Pre-merge edits are legitimate (sharpening the spec while the PR is
        # being written) and must NOT trip the gate.
        issue = MirrorIssue.from_dict(_issue_dict(last_edited_at='2026-04-17T10:00:00Z'))
        assert _classify_issue(issue) == 'solved'

    def test_issue_never_edited_is_solved(self):
        issue = MirrorIssue.from_dict(_issue_dict(last_edited_at=None))
        assert _classify_issue(issue) == 'solved'

    def test_missing_author_ignored(self):
        issue = MirrorIssue.from_dict(_issue_dict(author_github_id=None))
        assert _classify_issue(issue) == 'ignore'


# ============================================================================
# End-to-end scoring behavior
# ============================================================================


class TestRunMirrorIssueDiscovery:
    """End-to-end integration tests. Tests that expect a solving PR to be
    scorable pre-populate a ScoredMirrorPR on some miner's mirror_merged_prs
    so the cross-miner cache catches it — mimicking the real run order where
    OSS scoring populates these slots before issue discovery runs."""

    def test_no_mirror_repos_short_circuits(self):
        client = Mock()
        _run(run_mirror_issue_discovery({}, {}, _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client))
        client.get_miner_issues.assert_not_called()

    def test_miner_without_github_id_skipped(self):
        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict()])
        eval_ = _eval(github_id=None)
        _run(
            run_mirror_issue_discovery(
                {1: eval_},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )
        client.get_miner_issues.assert_not_called()
        assert eval_.total_solved_issues == 0

    def test_solved_issue_increments_counters(self):
        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict()])
        eval_ = _eval()
        # Pre-seed the solving PR into another miner's mirror_merged_prs so the cache hits
        seed_eval = MinerEvaluation(uid=2, hotkey='hk2', github_id='seed')
        seed_eval.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', 100)]

        _run(
            run_mirror_issue_discovery(
                {1: eval_, 2: seed_eval},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )
        assert eval_.total_solved_issues == 1
        assert eval_.total_valid_solved_issues == 1
        # No fetch needed — everything came from the cache
        client.get_pr_files.assert_not_called()

    def test_self_issue_counts_credibility_but_no_score(self):
        # author_github_id == solving_pr.author_github_id
        client = Mock()
        client.get_miner_issues.return_value = _response(
            [
                _issue_dict(author_github_id='SELF', solving_pr_author='SELF'),
            ]
        )
        eval_ = _eval()
        # Seed cache so cache-miss fetch isn't triggered
        eval_.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', 100)]

        _run(
            run_mirror_issue_discovery(
                {1: eval_},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )
        assert eval_.total_solved_issues == 1  # credibility counts
        # But no discovery_earned_score because self-solve
        assert eval_.issue_discovery_score == 0

    def test_not_planned_bumps_closed_count(self):
        client = Mock()
        client.get_miner_issues.return_value = _response(
            [
                _issue_dict(state_reason='NOT_PLANNED'),
            ]
        )
        eval_ = _eval()
        _run(
            run_mirror_issue_discovery(
                {1: eval_},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )
        assert eval_.total_solved_issues == 0
        assert eval_.total_closed_issues == 1

    def test_transferred_issue_ignored_entirely(self):
        client = Mock()
        client.get_miner_issues.return_value = _response(
            [
                _issue_dict(is_transferred=True),
            ]
        )
        eval_ = _eval()
        _run(
            run_mirror_issue_discovery(
                {1: eval_},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )
        assert eval_.total_solved_issues == 0
        assert eval_.total_closed_issues == 0

    def test_non_mirror_enabled_repo_filtered_out(self):
        client = Mock()
        client.get_miner_issues.return_value = _response(
            [
                _issue_dict(repo='foo/not-enabled'),
            ]
        )
        eval_ = _eval()
        _run(
            run_mirror_issue_discovery(
                {1: eval_},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )
        assert eval_.total_solved_issues == 0

    def test_mirror_request_error_does_not_abort_other_miners(self):
        client = Mock()

        def _per_miner(github_id, since=None):
            if github_id == 'fails':
                raise MirrorRequestError('boom')
            return _response([_issue_dict()])

        client.get_miner_issues.side_effect = _per_miner

        failing = MinerEvaluation(uid=1, hotkey='hk1', github_id='fails')
        working = MinerEvaluation(uid=2, hotkey='hk2', github_id='works')
        # Seed cache so working miner's solving PR is scoreable
        working.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', 100)]

        _run(
            run_mirror_issue_discovery(
                {1: failing, 2: working},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )
        assert failing.total_solved_issues == 0
        assert working.total_solved_issues == 1

    def test_mirror_request_error_restores_cached_issue_discovery_fields(self):
        cache = MinerEvaluationCache()
        cached = MinerEvaluation(uid=1, hotkey='hk1', github_id='fails')
        cached.issue_discovery_score = 8.12
        cached.issue_token_score = 700.0
        cached.issue_credibility = 1.0
        cached.is_issue_eligible = True
        cached.total_solved_issues = 7
        cached.total_valid_solved_issues = 7
        cache.store(cached)

        client = Mock()
        working_issues = [
            _issue_dict(issue_number=20 + i, author_github_id='B', solved_by_pr=300 + i) for i in range(7)
        ]

        def _per_miner(github_id, since=None):
            if github_id == 'fails':
                raise MirrorRequestError('boom')
            return _response(working_issues)

        client.get_miner_issues.side_effect = _per_miner

        failing = MinerEvaluation(uid=1, hotkey='hk1', github_id='fails')
        working = _eval(uid=2, github_id='works')
        working.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', pr) for pr in range(300, 307)]

        _run(
            run_mirror_issue_discovery(
                {1: failing, 2: working},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
                evaluation_cache=cache,
            )
        )

        assert failing.issue_discovery_score == 8.12
        assert failing.issue_token_score == 700.0
        assert failing.issue_credibility == 1.0
        assert failing.is_issue_eligible is True
        assert failing.total_solved_issues == 7
        assert failing.total_valid_solved_issues == 7
        assert working.issue_discovery_score > 0

        rewards = normalize_issue_discovery_rewards({1: failing, 2: working})
        assert rewards[1] < 1.0
        assert rewards[2] < 1.0
        assert sum(rewards.values()) == pytest.approx(1.0)

    def test_successful_issue_fetch_refreshes_cache_after_scoring(self):
        cache = MinerEvaluationCache()
        client = Mock()
        client.get_miner_issues.return_value = _response(
            [_issue_dict(issue_number=10 + i, author_github_id='A', solved_by_pr=200 + i) for i in range(7)]
        )

        eval_ = _eval(uid=1, github_id='999')
        eval_.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', pr) for pr in range(200, 207)]
        # Mimic the OSS-phase store that happens before issue discovery runs.
        # update_issue_discovery() only refreshes existing entries.
        cache.store(eval_)

        _run(
            run_mirror_issue_discovery(
                {1: eval_},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
                evaluation_cache=cache,
            )
        )

        cached = cache.get(uid=1, hotkey='hk', github_id='999')
        assert cached is not None
        assert cached.issue_discovery_score == eval_.issue_discovery_score
        assert cached.issue_discovery_score > 0
        assert cached.total_solved_issues == 7
        assert cached.total_valid_solved_issues == 7

    def test_oss_store_preserves_cached_issue_fields_across_rounds(self):
        """Regression: prior round's issue-discovery refresh must survive the
        next round's OSS-phase store() so a same-round mirror failure can
        restore the prior score. Without store()'s identity-match preserve
        logic, the fresh-eval store wipes the entry and the restore reads
        zeros — defeating the entire fallback (issue #1065)."""
        cache = MinerEvaluationCache()

        # --- Round N-1: full success.
        # OSS phase stores the eval. At OSS-phase time the eval has the
        # MinerEvaluation dataclass defaults for the issue-discovery fields
        # (all zero/False) because issue discovery has not run yet this round.
        round_n_minus_1 = _eval(uid=1, github_id='999')
        round_n_minus_1.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', pr) for pr in range(300, 307)]
        cache.store(round_n_minus_1)

        # Issue phase finishes and refreshes the cached issue-discovery fields.
        round_n_minus_1.issue_discovery_score = 8.12
        round_n_minus_1.issue_token_score = 700.0
        round_n_minus_1.issue_credibility = 1.0
        round_n_minus_1.is_issue_eligible = True
        round_n_minus_1.total_solved_issues = 7
        round_n_minus_1.total_valid_solved_issues = 7
        cache.update_issue_discovery(round_n_minus_1)

        # --- Round N: a fresh MinerEvaluation with all issue fields at
        # dataclass defaults. The OSS phase stores it. Without merge-on-store
        # this would clobber the round-N-1 refresh.
        round_n = _eval(uid=1, github_id='999')
        round_n.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', pr) for pr in range(300, 307)]
        cache.store(round_n)

        # Mirror fetch fails in round N. _restore_issue_discovery_from_cache
        # reads the entry that store() should have preserved.
        client = Mock()
        client.get_miner_issues.side_effect = MirrorRequestError('boom')

        _run(
            run_mirror_issue_discovery(
                {1: round_n},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
                evaluation_cache=cache,
            )
        )

        assert round_n.issue_discovery_score == 8.12
        assert round_n.issue_token_score == 700.0
        assert round_n.issue_credibility == 1.0
        assert round_n.is_issue_eligible is True
        assert round_n.total_solved_issues == 7
        assert round_n.total_valid_solved_issues == 7

    def test_successful_no_issue_fetch_clears_stale_cached_issue_fields(self):
        cache = MinerEvaluationCache()
        stale = _eval(uid=1, github_id='999')
        stale.issue_discovery_score = 8.12
        stale.issue_token_score = 700.0
        stale.issue_credibility = 1.0
        stale.is_issue_eligible = True
        stale.total_solved_issues = 7
        stale.total_valid_solved_issues = 7
        cache.store(stale)

        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict(repo='foo/not-enabled')])

        eval_ = _eval(uid=1, github_id='999')
        _run(
            run_mirror_issue_discovery(
                {1: eval_},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
                evaluation_cache=cache,
            )
        )

        assert eval_.issue_discovery_score == 0.0
        assert eval_.issue_token_score == 0.0
        assert eval_.issue_credibility == 0.0
        assert eval_.is_issue_eligible is False
        assert eval_.total_solved_issues == 0
        assert eval_.total_valid_solved_issues == 0

        cached = cache.get(uid=1, hotkey='hk', github_id='999')
        assert cached is not None
        assert cached.issue_discovery_score == 0.0
        assert cached.total_solved_issues == 0

    def test_solving_pr_file_fetch_failure_does_not_overwrite_cached_issue_fields(self):
        cache = MinerEvaluationCache()
        stale = _eval(uid=1, github_id='999')
        stale.issue_discovery_score = 8.12
        stale.issue_token_score = 700.0
        stale.issue_credibility = 1.0
        stale.is_issue_eligible = True
        stale.total_solved_issues = 7
        stale.total_valid_solved_issues = 7
        cache.store(stale)

        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict()])
        client.get_pr_files.side_effect = MirrorRequestError('files fetch failed')

        eval_ = _eval(uid=1, github_id='999')
        _run(
            run_mirror_issue_discovery(
                {1: eval_},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
                evaluation_cache=cache,
            )
        )

        assert eval_.issue_discovery_score == 0.0
        assert eval_.total_solved_issues == 1

        cached = cache.get(uid=1, hotkey='hk', github_id='999')
        assert cached is not None
        assert cached.issue_discovery_score == 8.12
        assert cached.total_solved_issues == 7


# ============================================================================
# Cache behavior
# ============================================================================


class TestSolvingPrCache:
    def test_build_cache_from_multiple_miners(self):
        e1 = MinerEvaluation(uid=1, hotkey='hk1', github_id='g1')
        e1.mirror_merged_prs = [_scored_mirror_pr('foo/a', 1, token_score=50, base_score=10)]
        e2 = MinerEvaluation(uid=2, hotkey='hk2', github_id='g2')
        e2.mirror_merged_prs = [_scored_mirror_pr('foo/b', 2, token_score=80, base_score=20)]

        cache = _build_solving_pr_cache({1: e1, 2: e2})
        assert cache[('foo/a', 1)].token_score == 50
        assert cache[('foo/a', 1)].base_score == 10
        assert cache[('foo/b', 2)].token_score == 80
        assert cache[('foo/b', 2)].base_score == 20

    def test_cache_first_occurrence_wins_on_duplicate(self):
        # If the same (repo, pr_number) somehow appears in two miners' lists
        # (shouldn't happen in practice but defensively tested), first wins.
        e1 = MinerEvaluation(uid=1, hotkey='hk1', github_id='g1')
        e1.mirror_merged_prs = [_scored_mirror_pr('foo/a', 1, token_score=50)]
        e2 = MinerEvaluation(uid=2, hotkey='hk2', github_id='g2')
        e2.mirror_merged_prs = [_scored_mirror_pr('foo/a', 1, token_score=99)]

        cache = _build_solving_pr_cache({1: e1, 2: e2})
        assert cache[('foo/a', 1)].token_score == 50  # first wins

    def test_below_threshold_prs_excluded_from_cache(self):
        e1 = MinerEvaluation(uid=1, hotkey='hk1', github_id='g1')
        e1.mirror_merged_prs = [
            _scored_mirror_pr('foo/poisoned', 1, token_score=0.0, base_score=0.0),
            _scored_mirror_pr('foo/healthy', 2, token_score=50, base_score=10),
        ]
        cache = _build_solving_pr_cache({1: e1})
        assert ('foo/poisoned', 1) not in cache
        assert ('foo/healthy', 2) in cache

    def test_cache_hit_reuses_base_score_no_fetch(self):
        """A solving PR already in cache must not trigger a get_pr_files call."""
        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict()])
        eval_ = _eval()
        # Seed cache via a second miner's mirror_merged_prs
        seed = MinerEvaluation(uid=2, hotkey='hk2', github_id='seed')
        seed.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', 100, base_score=42.0)]

        _run(
            run_mirror_issue_discovery(
                {1: eval_, 2: seed},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )

        client.get_pr_files.assert_not_called()
        # The cached token_score (100) flowed into issue_token_score
        assert eval_.issue_token_score == 100.0
        # And the issue counted toward valid_solved (token_score >= MIN threshold)
        assert eval_.total_valid_solved_issues == 1

    def test_cache_miss_fetches_and_writes_back(self):
        """A solving PR NOT in cache triggers one get_pr_files call."""
        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict()])
        client.get_pr_files.return_value = _empty_files_response('entrius/gittensor-ui', 100)

        eval_ = _eval()
        _run(
            run_mirror_issue_discovery(
                {1: eval_},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )

        assert client.get_pr_files.call_count == 1
        # Empty files → base_score 0 → no discovery score awarded; but issue still counted
        assert eval_.total_solved_issues == 1

    def test_cross_miner_cache_dedup(self):
        """Same non-miner solving PR closes issues for two miners → one fetch total."""
        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict()])
        client.get_pr_files.return_value = _empty_files_response('entrius/gittensor-ui', 100)

        e1 = _eval(uid=1, github_id='g1')
        e2 = _eval(uid=2, github_id='g2')
        _run(
            run_mirror_issue_discovery(
                {1: e1, 2: e2},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )

        # Both miners saw the same solving PR, but only one get_pr_files call fired
        assert client.get_pr_files.call_count == 1

    def test_fetch_failure_skips_scoring_for_that_issue(self):
        """MirrorRequestError on get_pr_files leaves the issue in solved_count
        but no discovery_earned_score is produced."""
        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict()])
        client.get_pr_files.side_effect = MirrorRequestError('files fetch failed')

        eval_ = _eval()
        _run(
            run_mirror_issue_discovery(
                {1: eval_},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )

        # Solved counts went up from _classify_issue before the fetch failed
        assert eval_.total_solved_issues == 1
        # But no discovery_earned_score — we don't reward unverifiable PRs
        assert eval_.issue_discovery_score == 0

    def test_token_score_below_threshold_counts_credibility_only(self):
        """Solving PR tokenizes to below MIN_TOKEN_SCORE_FOR_BASE_SCORE.
        total_solved_issues increments (credibility), but total_valid_solved_issues
        does not, and no discovery_earned_score is produced. Below-threshold PRs
        are excluded from the pre-cache, so this exercises the fresh-fetch path
        that re-tokenizes to 0 with empty files + empty token config."""
        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict()])
        client.get_pr_files.return_value = _empty_files_response('entrius/gittensor-ui', 100)
        eval_ = _eval()

        _run(
            run_mirror_issue_discovery(
                {1: eval_},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )

        assert eval_.total_solved_issues == 1  # credibility
        assert eval_.total_valid_solved_issues == 0  # below gate
        assert eval_.issue_discovery_score == 0


class TestCacheStats:
    """Verify the _CacheStats counter accurately tracks hits / misses /
    fetch failures across the mix of resolution paths."""

    def test_stats_dataclass_defaults_zero(self):
        from gittensor.validator.issue_discovery.mirror_scan import _CacheStats

        s = _CacheStats()
        assert s.hits == 0 and s.misses == 0 and s.fetch_failures == 0

    def test_resolve_increments_hit_on_cache_lookup(self):
        from gittensor.validator.issue_discovery.mirror_scan import (
            CachedSolvingPR,
            _CacheStats,
            _resolve_solving_pr_score,
        )

        client = Mock()
        cache = {('entrius/gittensor-ui', 100): CachedSolvingPR(base_score=42.0, token_score=50.0)}
        stats = _CacheStats()

        issue = MirrorIssue.from_dict(_issue_dict())
        assert issue.solving_pr is not None
        result = asyncio.run(
            _resolve_solving_pr_score(issue, issue.solving_pr, cache, stats, client, _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG)
        )

        assert result is not None
        assert result.base_score == 42.0
        assert stats.hits == 1
        assert stats.misses == 0
        client.get_pr_files.assert_not_called()

    def test_resolve_increments_miss_on_fetch_success(self):
        from gittensor.validator.issue_discovery.mirror_scan import (
            _CacheStats,
            _resolve_solving_pr_score,
        )

        client = Mock()
        client.get_pr_files.return_value = _empty_files_response('entrius/gittensor-ui', 100)
        cache = {}
        stats = _CacheStats()

        issue = MirrorIssue.from_dict(_issue_dict())
        asyncio.run(
            _resolve_solving_pr_score(issue, issue.solving_pr, cache, stats, client, _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG)
        )

        assert stats.hits == 0
        assert stats.misses == 1
        assert stats.fetch_failures == 0
        # Fetched result is now cached for future lookups
        assert ('entrius/gittensor-ui', 100) in cache

    def test_resolve_increments_fetch_failures_on_request_error(self):
        from gittensor.validator.issue_discovery.mirror_scan import (
            _CacheStats,
            _resolve_solving_pr_score,
        )

        client = Mock()
        client.get_pr_files.side_effect = MirrorRequestError('boom')
        cache = {}
        stats = _CacheStats()

        issue = MirrorIssue.from_dict(_issue_dict())
        result = asyncio.run(
            _resolve_solving_pr_score(issue, issue.solving_pr, cache, stats, client, _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG)
        )

        assert result is None
        assert stats.hits == 0
        assert stats.misses == 1
        assert stats.fetch_failures == 1
        # Failed lookups are NOT cached (so a retry is possible)
        assert cache == {}

    def test_resolve_treats_unavailable_scoring_data_as_failure(self):
        # scoring_data_stored=False is data-availability noise, not a real
        # zero score. Same handling as MirrorRequestError: increment
        # fetch_failures, return None, leave cache empty so a sibling miner's
        # later lookup can retry within the cycle.
        from gittensor.validator.issue_discovery.mirror_scan import (
            _CacheStats,
            _resolve_solving_pr_score,
        )

        client = Mock()
        client.get_pr_files.return_value = MirrorPullRequestFilesResponse.from_dict(
            {
                'repo_full_name': 'entrius/gittensor-ui',
                'pr_number': 100,
                'head_sha': 'h',
                'base_sha': 'b',
                'merge_base_sha': 'mb',
                'scoring_data_stored': False,
                'files': [],
            }
        )
        cache = {}
        stats = _CacheStats()

        issue = MirrorIssue.from_dict(_issue_dict())
        result = asyncio.run(
            _resolve_solving_pr_score(issue, issue.solving_pr, cache, stats, client, _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG)
        )

        assert result is None
        assert stats.misses == 1
        assert stats.fetch_failures == 1
        assert cache == {}

    def test_unavailable_scoring_data_is_not_cached_across_sibling_lookups(self):
        # Acceptance for issue #836: a single scoring_data_stored=False response
        # feeding two issues that share the same solving PR (i.e. across two
        # miners discovering the same PR) results in two misses, two fetch
        # failures, and an empty cache. Without the fix, the first call would
        # cache base_score=0 / token_score=0, the second would be a "hit" on
        # that fabricated zero, and fetch_failures would never increment.
        from gittensor.validator.issue_discovery.mirror_scan import (
            _CacheStats,
            _resolve_solving_pr_score,
        )

        client = Mock()
        client.get_pr_files.return_value = MirrorPullRequestFilesResponse.from_dict(
            {
                'repo_full_name': 'entrius/gittensor-ui',
                'pr_number': 100,
                'head_sha': 'h',
                'base_sha': 'b',
                'merge_base_sha': 'mb',
                'scoring_data_stored': False,
                'files': [],
            }
        )
        cache = {}
        stats = _CacheStats()

        issue_a = MirrorIssue.from_dict(_issue_dict(issue_number=50))
        issue_b = MirrorIssue.from_dict(_issue_dict(issue_number=51))

        result_a = asyncio.run(
            _resolve_solving_pr_score(
                issue_a, issue_a.solving_pr, cache, stats, client, _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG
            )
        )
        result_b = asyncio.run(
            _resolve_solving_pr_score(
                issue_b, issue_b.solving_pr, cache, stats, client, _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG
            )
        )

        assert result_a is None
        assert result_b is None
        assert stats.hits == 0
        assert stats.misses == 2
        assert stats.fetch_failures == 2
        assert cache == {}
        assert client.get_pr_files.call_count == 2


class TestOpenIssueSpamSourceIsMirror:
    """The open-issue spam multiplier sources its count from mirror's response,
    and mirror_scan also writes that count to evaluation.total_open_issues so
    the DB row reflects mirror-scoped state."""

    def test_all_mirror_miner_with_many_open_issues_trips_spam(self):
        """6 open issues in mirror response trips the spam multiplier."""
        open_issues = [
            _issue_dict(
                issue_number=200 + i,
                state='OPEN',
                state_reason=None,
                solved_by_pr=None,
            )
            for i in range(6)
        ]
        # Plus a few solved issues so the miner has some scoreable work + valid count.
        solved_issues = [_issue_dict(issue_number=300 + i, author_github_id=f'discoverer{i}') for i in range(8)]
        client = Mock()
        client.get_miner_issues.return_value = _response(open_issues + solved_issues)

        eval_ = _eval()
        # Pre-seed cache with high-token solving PR so issues clear valid gate
        eval_.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', 100, token_score=100.0)]

        _run(
            run_mirror_issue_discovery(
                {1: eval_},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )

        # 6 open issues > threshold (5) → spam_mult=0 → all scored issues earn 0
        assert eval_.issue_discovery_score == 0
        # mirror_scan now records the mirror-scoped open count
        assert eval_.total_open_issues == 6

    def test_all_mirror_miner_below_threshold_passes_spam(self):
        """Fewer open issues, spam gate doesn't trip; field still recorded."""
        # Only 2 open issues (well under threshold 5)
        open_issues = [
            _issue_dict(
                issue_number=200 + i,
                state='OPEN',
                state_reason=None,
                solved_by_pr=None,
            )
            for i in range(2)
        ]
        solved_issues = [_issue_dict(issue_number=300 + i, author_github_id=f'discoverer{i}') for i in range(8)]
        client = Mock()
        client.get_miner_issues.return_value = _response(open_issues + solved_issues)

        eval_ = _eval()
        eval_.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', 100, token_score=100.0)]

        _run(
            run_mirror_issue_discovery(
                {1: eval_},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )

        # Below threshold → spam_mult=1.0 → discovery score is non-zero
        assert eval_.issue_discovery_score > 0
        assert eval_.total_open_issues == 2


class TestCrossMinerOneIssuePerPr:
    """Regression tests for the cross-miner one-issue-per-PR rule.

    A single solving PR closing issues authored by multiple miners must award
    discovery score to at most one of them — the earliest-created qualifying
    issue across all miners — with the rest counted as credibility-only. This
    matches the rule documented at the top of ``mirror_scan`` and the legacy
    pre-#796 behavior in ``_collect_issues_from_prs``.
    """

    def test_canonical_picks_earliest_created_across_miners(self):
        """``_build_canonical_pr_owners`` keys (repo, pr_number) to the
        earliest-created qualifying issue across all miners' fetches."""
        from gittensor.validator.issue_discovery.mirror_scan import _build_canonical_pr_owners

        e_a = _eval(uid=1, github_id='A')
        e_b = _eval(uid=2, github_id='B')

        a_issue = MirrorIssue.from_dict(
            _issue_dict(
                issue_number=50,
                author_github_id='A',
                solving_pr_author='SOLVER',
                created_at='2026-04-01T00:00:00Z',
            )
        )
        b_issue = MirrorIssue.from_dict(
            _issue_dict(
                issue_number=51,
                author_github_id='B',
                solving_pr_author='SOLVER',
                created_at='2026-04-05T00:00:00Z',
            )
        )

        canonical = _build_canonical_pr_owners([(e_a, [a_issue], 0), (e_b, [b_issue], 0)])

        # Earlier-created issue (#50, uid 1) wins canonical for PR 100
        owner = canonical[('entrius/gittensor-ui', 100)]
        assert owner[1] == 50
        assert owner[2] == 1

    def test_canonical_tie_break_lower_issue_number(self):
        """Identical ``created_at`` across miners → lower issue_number wins."""
        from gittensor.validator.issue_discovery.mirror_scan import _build_canonical_pr_owners

        # uid 2 first in iteration order, but uid 1's lower issue_number must win.
        e_a = _eval(uid=2, github_id='A')
        e_b = _eval(uid=1, github_id='B')

        a_issue = MirrorIssue.from_dict(
            _issue_dict(
                issue_number=51,
                author_github_id='A',
                solving_pr_author='SOLVER',
                created_at='2026-04-01T00:00:00Z',
            )
        )
        b_issue = MirrorIssue.from_dict(
            _issue_dict(
                issue_number=50,
                author_github_id='B',
                solving_pr_author='SOLVER',
                created_at='2026-04-01T00:00:00Z',
            )
        )

        canonical = _build_canonical_pr_owners([(e_a, [a_issue], 0), (e_b, [b_issue], 0)])

        owner = canonical[('entrius/gittensor-ui', 100)]
        assert owner[1] == 50  # lower issue_number wins
        assert owner[2] == 1  # ... which is uid 1 (e_b)

    def test_canonical_excludes_same_account(self):
        """Same-account issues never claim canonical ownership of a PR slot,
        leaving non-same-account siblings on the same PR free to score."""
        from gittensor.validator.issue_discovery.mirror_scan import _build_canonical_pr_owners

        e_a = _eval(uid=1, github_id='A')
        e_b = _eval(uid=2, github_id='B')

        # A's issue is same-account (author == solver) and earlier — must be excluded.
        a_issue = MirrorIssue.from_dict(
            _issue_dict(
                issue_number=50,
                author_github_id='A',
                solving_pr_author='A',
                created_at='2026-04-01T00:00:00Z',
            )
        )
        b_issue = MirrorIssue.from_dict(
            _issue_dict(
                issue_number=51,
                author_github_id='B',
                solving_pr_author='SOLVER',
                created_at='2026-04-05T00:00:00Z',
            )
        )

        canonical = _build_canonical_pr_owners([(e_a, [a_issue], 0), (e_b, [b_issue], 0)])

        owner = canonical[('entrius/gittensor-ui', 100)]
        assert owner[1] == 51  # B's issue claims canonical
        assert owner[2] == 2

    def test_two_miners_shared_pr_only_earliest_scores(self):
        """End-to-end: two miners each with 7 valid solved issues clearing
        the eligibility gate, one solving PR shared between them. The
        earlier-created issue's miner pockets the shared PR's contribution;
        the later one gets credibility only.
        """
        client = Mock()

        # 6 unique-PR issues + 1 shared-PR issue per miner. A's #50 is earlier
        # (April 1) than B's #51 (April 5), so A is canonical for PR 100.
        a_issues = [_issue_dict(issue_number=10 + i, author_github_id='A', solved_by_pr=200 + i) for i in range(6)]
        a_issues.append(
            _issue_dict(
                issue_number=50,
                author_github_id='A',
                solved_by_pr=100,
                solving_pr_author='SOLVER',
                created_at='2026-04-01T00:00:00Z',
            )
        )
        b_issues = [_issue_dict(issue_number=20 + i, author_github_id='B', solved_by_pr=300 + i) for i in range(6)]
        b_issues.append(
            _issue_dict(
                issue_number=51,
                author_github_id='B',
                solved_by_pr=100,
                solving_pr_author='SOLVER',
                created_at='2026-04-05T00:00:00Z',
            )
        )

        def _per_miner(github_id, since=None):
            return _response(a_issues if github_id == 'A' else b_issues)

        client.get_miner_issues.side_effect = _per_miner

        e_a = _eval(uid=1, github_id='A')
        e_b = _eval(uid=2, github_id='B')

        # Pre-seed cross-miner solving-PR cache so no fetches are needed.
        seed = MinerEvaluation(uid=99, hotkey='hkS', github_id='SEED')
        seed.mirror_merged_prs = [
            _scored_mirror_pr('entrius/gittensor-ui', pr_number)
            for pr_number in [100] + list(range(200, 206)) + list(range(300, 306))
        ]

        _run(
            run_mirror_issue_discovery(
                {1: e_a, 2: e_b, 99: seed},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )

        # Both miners count the shared-PR issue toward credibility.
        assert e_a.total_solved_issues == 7
        assert e_b.total_solved_issues == 7
        assert e_a.total_valid_solved_issues == 7
        assert e_b.total_valid_solved_issues == 7
        assert e_a.is_issue_eligible
        assert e_b.is_issue_eligible

        # ``issue_token_score`` accumulates only over SCORED PRs (default
        # ``_scored_mirror_pr`` token_score is 100.0), so this is the
        # deterministic, time-decay-independent check: A has 7 scored, B has
        # 6 (shared PR 100 is canonical for A only and credibility-only for B).
        assert e_a.issue_token_score == 700.0
        assert e_b.issue_token_score == 600.0
        # All solving PRs share identical scoring inputs at this issue mix, so
        # the discovery_score ratio collapses to 7:6.
        assert e_a.issue_discovery_score > e_b.issue_discovery_score > 0
        assert e_a.issue_discovery_score / e_b.issue_discovery_score == pytest.approx(7 / 6, rel=1e-2)

    def test_within_miner_one_issue_per_pr_still_holds(self):
        """One miner authoring two issues both closed by the same PR — the
        earlier-created issue scores; the later one is credibility-only.
        Preserves the original within-miner one-issue-per-PR semantics now
        that the rule is enforced via the cross-miner canonical map."""
        client = Mock()

        miner_issues = [
            _issue_dict(issue_number=10 + i, author_github_id='999', solved_by_pr=200 + i) for i in range(6)
        ]
        miner_issues.extend(
            [
                _issue_dict(
                    issue_number=50,
                    author_github_id='999',
                    solved_by_pr=100,
                    solving_pr_author='SOLVER',
                    created_at='2026-04-01T00:00:00Z',
                ),
                _issue_dict(
                    issue_number=51,
                    author_github_id='999',
                    solved_by_pr=100,
                    solving_pr_author='SOLVER',
                    created_at='2026-04-05T00:00:00Z',
                ),
            ]
        )

        client.get_miner_issues.return_value = _response(miner_issues)
        eval_ = _eval(uid=1, github_id='999')

        seed = MinerEvaluation(uid=99, hotkey='hkS', github_id='SEED')
        seed.mirror_merged_prs = [
            _scored_mirror_pr('entrius/gittensor-ui', pr_number) for pr_number in [100] + list(range(200, 206))
        ]

        _run(
            run_mirror_issue_discovery(
                {1: eval_, 99: seed},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )

        # 8 solved (both shared-PR issues counted for credibility), eligible.
        assert eval_.total_solved_issues == 8
        assert eval_.total_valid_solved_issues == 8
        assert eval_.is_issue_eligible
        # ``issue_token_score`` only accumulates over SCORED PRs (default
        # ``_scored_mirror_pr`` token_score is 100.0). 7 distinct scoring PRs
        # ⇒ 700.0; the later PR-100 issue is credibility-only and contributes
        # no token_score, so this would be 800.0 if the within-miner rule had
        # broken alongside the cross-miner one.
        assert eval_.issue_token_score == 700.0
        assert eval_.issue_discovery_score > 0

    def test_different_solving_prs_both_miners_score(self):
        """Two miners' issues closed by completely disjoint solving PRs —
        no cross-miner canonical contention; both miners score normally."""
        client = Mock()

        a_issues = [_issue_dict(issue_number=10 + i, author_github_id='A', solved_by_pr=200 + i) for i in range(7)]
        b_issues = [_issue_dict(issue_number=20 + i, author_github_id='B', solved_by_pr=300 + i) for i in range(7)]

        def _per_miner(github_id, since=None):
            return _response(a_issues if github_id == 'A' else b_issues)

        client.get_miner_issues.side_effect = _per_miner

        e_a = _eval(uid=1, github_id='A')
        e_b = _eval(uid=2, github_id='B')

        seed = MinerEvaluation(uid=99, hotkey='hkS', github_id='SEED')
        seed.mirror_merged_prs = [
            _scored_mirror_pr('entrius/gittensor-ui', pr_number)
            for pr_number in list(range(200, 207)) + list(range(300, 307))
        ]

        _run(
            run_mirror_issue_discovery(
                {1: e_a, 2: e_b, 99: seed},
                _mirror_repos('entrius/gittensor-ui'),
                _EMPTY_LANGS,
                _EMPTY_TOKEN_CONFIG,
                client=client,
            )
        )

        # Identical issue mix and disjoint PRs → identical scores. Both miners
        # score all 7 of their issues (no canonical contention).
        assert e_a.total_solved_issues == 7
        assert e_b.total_solved_issues == 7
        assert e_a.issue_token_score == 700.0
        assert e_b.issue_token_score == 700.0
        assert e_a.issue_discovery_score == e_b.issue_discovery_score
        assert e_a.issue_discovery_score > 0
