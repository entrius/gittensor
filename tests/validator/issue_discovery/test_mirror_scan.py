"""Unit tests for run_mirror_issue_discovery.

Focus: anti-gaming gates fire correctly, bucketing between solved / closed /
ignored, and the per-miner MinerEvaluation issue fields get populated.
"""

import asyncio
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
scored_pr_module = pytest.importorskip(
    'gittensor.validator.oss_contributions.mirror.scored_pr'
)

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
RepositoryConfig = load_weights.RepositoryConfig
TokenConfig = load_weights.TokenConfig
ScoredMirrorPR = scored_pr_module.ScoredMirrorPR


# Representative defaults for the plumbed-through token scoring args. The
# tests below populate the cache directly for cache-hit paths, so these are
# only used on cache-miss fetches (and even then MirrorPullRequestFilesResponse
# is mocked so no real token scoring math runs).
_EMPTY_LANGS = {}
_EMPTY_TOKEN_CONFIG = TokenConfig()


def _scored_mirror_pr(repo: str, pr_number: int, token_score: float = 100.0, base_score: float = 42.0) -> ScoredMirrorPR:
    """Build a ScoredMirrorPR for cache pre-population in tests."""
    pr = MirrorPullRequest.from_dict({
        'repo_full_name': repo, 'pr_number': pr_number,
        'title': 't', 'body': 'b', 'state': 'MERGED',
        'author_github_id': '1', 'author_login': 'a',
        'author_association': 'CONTRIBUTOR',
        'created_at': '2026-04-10T00:00:00Z',
        'closed_at': '2026-04-18T10:00:00Z',
        'merged_at': '2026-04-18T10:00:00Z',
        'last_edited_at': None,
        'edited_after_merge': False, 'hours_since_merge': 1.0,
        'merged_by_login': 'm',
        'base_ref': 'test',
        'head_sha': 'h', 'base_sha': 'b', 'merge_base_sha': 'mb',
        'additions': 1, 'deletions': 0, 'commits_count': 1,
        'scoring_data_stored': True,
        'review_summary': {'maintainer_changes_requested_count': 0},
        'labels': [], 'linked_issues': [],
    })
    scored = ScoredMirrorPR(pr=pr)
    scored.token_score = token_score
    scored.base_score = base_score
    return scored


def _empty_files_response(repo: str, pr_number: int) -> MirrorPullRequestFilesResponse:
    return MirrorPullRequestFilesResponse.from_dict({
        'repo_full_name': repo, 'pr_number': pr_number,
        'head_sha': 'h', 'base_sha': 'b', 'merge_base_sha': 'mb',
        'scoring_data_stored': True,
        'files': [],
    })


def _issue_dict(
    issue_number: int = 50,
    state: str = 'CLOSED',
    state_reason: str | None = 'COMPLETED',
    author_github_id: str = '999',
    is_transferred: bool = False,
    solved_by_pr: int | None = 100,
    solving_pr_state: str = 'MERGED',
    solving_pr_author: str = '218712309',
    solving_pr_edited_after_merge: bool = False,
    repo: str = 'entrius/gittensor-ui',
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
            'head_sha': 'h', 'base_sha': 'b', 'merge_base_sha': 'mb',
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
        'created_at': '2026-04-01T00:00:00Z',
        'closed_at': '2026-04-18T10:00:00Z' if state == 'CLOSED' else None,
        'updated_at': '2026-04-18T10:00:00Z',
        'last_edited_at': None,
        'is_transferred': is_transferred,
        'solved_by_pr': solved_by_pr,
        'labels': [],
        'solving_pr': sp,
    }


def _response(issue_dicts: list) -> MirrorIssuesResponse:
    return MirrorIssuesResponse.from_dict({
        'github_id': '999',
        'since': '2026-03-15T00:00:00Z',
        'generated_at': '2026-04-21T00:00:00Z',
        'issues': issue_dicts,
    })


def _eval(uid=1, github_id='999'):
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
        _run(run_mirror_issue_discovery(
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))
        client.get_miner_issues.assert_not_called()
        assert eval_.total_solved_issues == 0

    def test_solved_issue_increments_counters(self):
        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict()])
        eval_ = _eval()
        # Pre-seed the solving PR into another miner's mirror_merged_prs so the cache hits
        seed_eval = MinerEvaluation(uid=2, hotkey='hk2', github_id='seed')
        seed_eval.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', 100)]

        _run(run_mirror_issue_discovery(
            {1: eval_, 2: seed_eval}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))
        assert eval_.total_solved_issues == 1
        assert eval_.total_valid_solved_issues == 1
        # No fetch needed — everything came from the cache
        client.get_pr_files.assert_not_called()

    def test_self_issue_counts_credibility_but_no_score(self):
        # author_github_id == solving_pr.author_github_id
        client = Mock()
        client.get_miner_issues.return_value = _response([
            _issue_dict(author_github_id='SELF', solving_pr_author='SELF'),
        ])
        eval_ = _eval()
        # Seed cache so cache-miss fetch isn't triggered
        eval_.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', 100)]

        _run(run_mirror_issue_discovery(
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))
        assert eval_.total_solved_issues == 1  # credibility counts
        # But no discovery_earned_score because self-solve
        assert eval_.issue_discovery_score == 0

    def test_not_planned_bumps_closed_count(self):
        client = Mock()
        client.get_miner_issues.return_value = _response([
            _issue_dict(state_reason='NOT_PLANNED'),
        ])
        eval_ = _eval()
        _run(run_mirror_issue_discovery(
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))
        assert eval_.total_solved_issues == 0
        assert eval_.total_closed_issues == 1

    def test_transferred_issue_ignored_entirely(self):
        client = Mock()
        client.get_miner_issues.return_value = _response([
            _issue_dict(is_transferred=True),
        ])
        eval_ = _eval()
        _run(run_mirror_issue_discovery(
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))
        assert eval_.total_solved_issues == 0
        assert eval_.total_closed_issues == 0

    def test_non_mirror_enabled_repo_filtered_out(self):
        client = Mock()
        client.get_miner_issues.return_value = _response([
            _issue_dict(repo='foo/not-enabled'),
        ])
        eval_ = _eval()
        _run(run_mirror_issue_discovery(
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))
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

        _run(run_mirror_issue_discovery(
            {1: failing, 2: working}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))
        assert failing.total_solved_issues == 0
        assert working.total_solved_issues == 1


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

    def test_cache_hit_reuses_base_score_no_fetch(self):
        """A solving PR already in cache must not trigger a get_pr_files call."""
        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict()])
        eval_ = _eval()
        # Seed cache via a second miner's mirror_merged_prs
        seed = MinerEvaluation(uid=2, hotkey='hk2', github_id='seed')
        seed.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', 100, base_score=42.0)]

        _run(run_mirror_issue_discovery(
            {1: eval_, 2: seed}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))

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
        _run(run_mirror_issue_discovery(
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))

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
        _run(run_mirror_issue_discovery(
            {1: e1, 2: e2}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))

        # Both miners saw the same solving PR, but only one get_pr_files call fired
        assert client.get_pr_files.call_count == 1

    def test_fetch_failure_skips_scoring_for_that_issue(self):
        """MirrorRequestError on get_pr_files leaves the issue in solved_count
        but no discovery_earned_score is produced."""
        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict()])
        client.get_pr_files.side_effect = MirrorRequestError('files fetch failed')

        eval_ = _eval()
        _run(run_mirror_issue_discovery(
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))

        # Solved counts went up from _classify_issue before the fetch failed
        assert eval_.total_solved_issues == 1
        # But no discovery_earned_score — we don't reward unverifiable PRs
        assert eval_.issue_discovery_score == 0

    def test_token_score_below_threshold_counts_credibility_only(self):
        """Cached solving PR has token_score < MIN_TOKEN_SCORE_FOR_BASE_SCORE.
        total_solved_issues increments (credibility), but total_valid_solved_issues
        does not, and no discovery_earned_score is produced."""
        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict()])
        eval_ = _eval()
        # Token score 2 is below the threshold (5)
        eval_.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', 100, token_score=2.0)]

        _run(run_mirror_issue_discovery(
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))

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
            _CacheStats,
            _resolve_solving_pr_score,
            CachedSolvingPR,
        )
        client = Mock()
        cache = {('entrius/gittensor-ui', 100): CachedSolvingPR(base_score=42.0, token_score=50.0)}
        stats = _CacheStats()

        issue = MirrorIssue.from_dict(_issue_dict())
        result = _resolve_solving_pr_score(
            issue, issue.solving_pr, cache, stats, client, _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG
        )

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
        _resolve_solving_pr_score(
            issue, issue.solving_pr, cache, stats, client, _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG
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
        result = _resolve_solving_pr_score(
            issue, issue.solving_pr, cache, stats, client, _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG
        )

        assert result is None
        assert stats.hits == 0
        assert stats.misses == 1
        assert stats.fetch_failures == 1
        # Failed lookups are NOT cached (so a retry is possible)
        assert cache == {}


class TestOpenIssueSpamSourceIsMirror:
    """The open-issue spam multiplier should source its count from mirror's
    response, NOT evaluation.total_open_issues which only legacy fills.
    Otherwise an all-mirror miner gets a free pass on the spam gate."""

    def test_all_mirror_miner_with_many_open_issues_trips_spam(self):
        """Miner with no legacy load (total_open_issues stays 0) but many open
        issues in the mirror response should still trip the spam multiplier."""
        # 6 open issues (above OPEN_ISSUE_SPAM_BASE_THRESHOLD=5 with 0 token bonus).
        # All open + same-account so they don't get scored either way — point is
        # the spam gate triggers based on mirror data, not legacy total_open_issues.
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
        solved_issues = [
            _issue_dict(issue_number=300 + i, author_github_id=f'discoverer{i}')
            for i in range(8)
        ]
        client = Mock()
        client.get_miner_issues.return_value = _response(open_issues + solved_issues)

        eval_ = _eval()
        # Pre-seed cache with high-token solving PR so issues clear valid gate
        eval_.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', 100, token_score=100.0)]
        # Critical: total_open_issues stays at default 0 (legacy load never ran)
        assert eval_.total_open_issues == 0

        _run(run_mirror_issue_discovery(
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))

        # 6 open issues > threshold (5) → spam_mult=0 → all scored issues earn 0
        assert eval_.issue_discovery_score == 0

    def test_all_mirror_miner_below_threshold_passes_spam(self):
        """Same miner, fewer open issues, spam gate doesn't trip."""
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
        solved_issues = [
            _issue_dict(issue_number=300 + i, author_github_id=f'discoverer{i}')
            for i in range(8)
        ]
        client = Mock()
        client.get_miner_issues.return_value = _response(open_issues + solved_issues)

        eval_ = _eval()
        eval_.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', 100, token_score=100.0)]

        _run(run_mirror_issue_discovery(
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))

        # Below threshold → spam_mult=1.0 → discovery score is non-zero
        assert eval_.issue_discovery_score > 0

    def test_legacy_total_open_issues_value_is_ignored(self):
        """Confirms mirror_scan does NOT read evaluation.total_open_issues. Setting
        it to a huge value should NOT trip the spam gate when mirror's count is low."""
        open_issues = [
            _issue_dict(
                issue_number=200 + i,
                state='OPEN',
                state_reason=None,
                solved_by_pr=None,
            )
            for i in range(2)  # only 2 open in mirror — below threshold
        ]
        solved_issues = [
            _issue_dict(issue_number=300 + i, author_github_id=f'discoverer{i}')
            for i in range(8)
        ]
        client = Mock()
        client.get_miner_issues.return_value = _response(open_issues + solved_issues)

        eval_ = _eval()
        eval_.total_open_issues = 999  # legacy says 999 — should be ignored
        eval_.mirror_merged_prs = [_scored_mirror_pr('entrius/gittensor-ui', 100, token_score=100.0)]

        _run(run_mirror_issue_discovery(
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'),
            _EMPTY_LANGS, _EMPTY_TOKEN_CONFIG, client=client,
        ))

        # Mirror's count of 2 (under threshold) wins; spam doesn't trip
        assert eval_.issue_discovery_score > 0
