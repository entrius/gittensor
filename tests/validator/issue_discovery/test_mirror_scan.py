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

run_mirror_issue_discovery = mirror_scan_module.run_mirror_issue_discovery
_classify_issue = mirror_scan_module._classify_issue
MirrorIssue = mirror_models.MirrorIssue
MirrorIssuesResponse = mirror_models.MirrorIssuesResponse
MirrorRequestError = mirror_client_mod.MirrorRequestError
MinerEvaluation = classes.MinerEvaluation
RepositoryConfig = load_weights.RepositoryConfig


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
    def test_no_mirror_repos_short_circuits(self):
        client = Mock()
        _run(run_mirror_issue_discovery({}, {}, client=client))
        client.get_miner_issues.assert_not_called()

    def test_miner_without_github_id_skipped(self):
        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict()])
        eval_ = _eval(github_id=None)
        _run(run_mirror_issue_discovery({1: eval_}, _mirror_repos('entrius/gittensor-ui'), client=client))
        client.get_miner_issues.assert_not_called()
        assert eval_.total_solved_issues == 0

    def test_solved_issue_increments_counters(self):
        client = Mock()
        client.get_miner_issues.return_value = _response([_issue_dict()])
        eval_ = _eval()
        _run(run_mirror_issue_discovery(
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'), client=client
        ))
        assert eval_.total_solved_issues == 1
        assert eval_.total_valid_solved_issues == 1

    def test_self_issue_counts_credibility_but_no_score(self):
        # author_github_id == solving_pr.author_github_id
        client = Mock()
        client.get_miner_issues.return_value = _response([
            _issue_dict(author_github_id='SELF', solving_pr_author='SELF'),
        ])
        eval_ = _eval()
        _run(run_mirror_issue_discovery(
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'), client=client
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
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'), client=client
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
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'), client=client
        ))
        assert eval_.total_solved_issues == 0
        assert eval_.total_closed_issues == 0

    def test_non_mirror_enabled_repo_filtered_out(self):
        client = Mock()
        # Response contains an issue in a repo not in the enabled set
        client.get_miner_issues.return_value = _response([
            _issue_dict(repo='foo/not-enabled'),
        ])
        eval_ = _eval()
        _run(run_mirror_issue_discovery(
            {1: eval_}, _mirror_repos('entrius/gittensor-ui'), client=client
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
        _run(run_mirror_issue_discovery(
            {1: failing, 2: working}, _mirror_repos('entrius/gittensor-ui'), client=client
        ))
        assert failing.total_solved_issues == 0
        assert working.total_solved_issues == 1
