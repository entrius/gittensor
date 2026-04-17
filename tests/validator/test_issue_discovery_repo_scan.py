# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for repo_scan: state_reason population from REST state_reason field.

REST returns lowercase (`transferred`, `not_planned`, `completed`); repo_scan
normalizes to uppercase for a single gate with the GraphQL path.
"""

import asyncio
from typing import Optional

from gittensor.validator.issue_discovery import repo_scan


def _make_raw(state_reason: Optional[str], number: int) -> dict:
    return {
        'number': number,
        'title': f'issue {number}',
        'created_at': '2026-01-01T00:00:00Z',
        'closed_at': '2026-01-02T00:00:00Z',
        'state': 'closed',
        'state_reason': state_reason,
        'user': {'login': 'alice', 'id': 1001},
    }


def _run_scan(monkeypatch, raw_issues):
    monkeypatch.setattr(
        repo_scan,
        '_fetch_closed_issues',
        lambda repo_name, since, token: raw_issues,
    )
    monkeypatch.setattr(
        repo_scan,
        'find_solver_from_cross_references',
        lambda repo, issue_number, token: (None, None),
    )

    result: dict = {}
    asyncio.get_event_loop().run_until_complete(
        repo_scan._scan_repo(
            repo_name='test/repo',
            lookback_date='2026-01-01T00:00:00Z',
            validator_pat='x',
            miner_github_ids={'1001'},
            known_issues=set(),
            result=result,
            lookup_cap=10,
        )
    )
    return result


def test_scan_repo_sets_state_reason_for_transferred_issue(monkeypatch):
    result = _run_scan(monkeypatch, [_make_raw('transferred', 42)])

    assert '1001' in result
    assert result['1001'][0].state_reason == 'TRANSFERRED'
    assert result['1001'][0].is_transferred is True


def test_scan_repo_sets_state_reason_for_not_planned_issue(monkeypatch):
    result = _run_scan(monkeypatch, [_make_raw('not_planned', 43)])

    assert '1001' in result
    assert result['1001'][0].state_reason == 'NOT_PLANNED'
    assert result['1001'][0].is_transferred is False


def test_scan_repo_sets_state_reason_for_completed_issue(monkeypatch):
    result = _run_scan(monkeypatch, [_make_raw('completed', 44)])

    assert '1001' in result
    assert result['1001'][0].state_reason == 'COMPLETED'
    assert result['1001'][0].is_transferred is False


def test_scan_repo_sets_state_reason_none_when_missing(monkeypatch):
    """Legacy issues closed before GitHub rolled out state_reason return None."""
    result = _run_scan(monkeypatch, [_make_raw(None, 45)])

    assert '1001' in result
    assert result['1001'][0].state_reason is None
    assert result['1001'][0].is_transferred is False


# -- Solver lookup budget: pre-filter by state_reason ----------------------


def test_non_completed_issues_skip_solver_lookup(monkeypatch):
    """NOT_PLANNED and TRANSFERRED issues must not consume solver lookups."""
    lookup_calls: list = []

    def _tracking_solver(repo, issue_number, token):
        lookup_calls.append(issue_number)
        return None, None

    monkeypatch.setattr(
        repo_scan,
        '_fetch_closed_issues',
        lambda *a: [
            _make_raw('not_planned', 10),
            _make_raw('transferred', 11),
        ],
    )
    monkeypatch.setattr(repo_scan, 'find_solver_from_cross_references', _tracking_solver)

    result: dict = {}
    asyncio.get_event_loop().run_until_complete(
        repo_scan._scan_repo(
            repo_name='test/repo',
            lookback_date='2026-01-01T00:00:00Z',
            validator_pat='x',
            miner_github_ids={'1001'},
            known_issues=set(),
            result=result,
            lookup_cap=10,
        )
    )

    # Issues should be in the result (for credibility counting)
    assert len(result.get('1001', [])) == 2
    # But NO solver lookups should have been performed
    assert lookup_calls == [], f'Expected no lookups, got {lookup_calls}'


def test_completed_issues_still_get_solver_lookup(monkeypatch):
    """COMPLETED issues must still go through the solver lookup path."""
    lookup_calls: list = []

    def _tracking_solver(repo, issue_number, token):
        lookup_calls.append(issue_number)
        return 9999, issue_number  # simulate a solver found

    monkeypatch.setattr(
        repo_scan,
        '_fetch_closed_issues',
        lambda *a: [
            _make_raw('completed', 20),
            _make_raw('completed', 21),
        ],
    )
    monkeypatch.setattr(repo_scan, 'find_solver_from_cross_references', _tracking_solver)

    result: dict = {}
    lookups = asyncio.get_event_loop().run_until_complete(
        repo_scan._scan_repo(
            repo_name='test/repo',
            lookback_date='2026-01-01T00:00:00Z',
            validator_pat='x',
            miner_github_ids={'1001'},
            known_issues=set(),
            result=result,
            lookup_cap=10,
        )
    )

    assert len(result.get('1001', [])) == 2
    assert sorted(lookup_calls) == [20, 21]
    assert lookups == 2


def test_mixed_state_reasons_only_completed_use_budget(monkeypatch):
    """Mixed batch: only COMPLETED issues consume solver lookup budget."""
    lookup_calls: list = []

    def _tracking_solver(repo, issue_number, token):
        lookup_calls.append(issue_number)
        return None, None

    monkeypatch.setattr(
        repo_scan,
        '_fetch_closed_issues',
        lambda *a: [
            _make_raw('completed', 30),
            _make_raw('not_planned', 31),
            _make_raw('completed', 32),
            _make_raw('transferred', 33),
            _make_raw(None, 34),  # legacy null
        ],
    )
    monkeypatch.setattr(repo_scan, 'find_solver_from_cross_references', _tracking_solver)

    result: dict = {}
    lookups = asyncio.get_event_loop().run_until_complete(
        repo_scan._scan_repo(
            repo_name='test/repo',
            lookback_date='2026-01-01T00:00:00Z',
            validator_pat='x',
            miner_github_ids={'1001'},
            known_issues=set(),
            result=result,
            lookup_cap=10,
        )
    )

    all_issues = result.get('1001', [])
    assert len(all_issues) == 5, f'All 5 issues should be in result, got {len(all_issues)}'
    # Only the 2 COMPLETED issues should have triggered solver lookups
    assert sorted(lookup_calls) == [30, 32]
    assert lookups == 2


def test_lookup_budget_not_charged_for_non_completed(monkeypatch):
    """Return value (lookup count) must only reflect COMPLETED solver calls."""
    monkeypatch.setattr(
        repo_scan,
        '_fetch_closed_issues',
        lambda *a: [
            _make_raw('not_planned', 40),
            _make_raw('transferred', 41),
            _make_raw(None, 42),
        ],
    )
    monkeypatch.setattr(repo_scan, 'find_solver_from_cross_references', lambda *a: (None, None))

    result: dict = {}
    lookups = asyncio.get_event_loop().run_until_complete(
        repo_scan._scan_repo(
            repo_name='test/repo',
            lookback_date='2026-01-01T00:00:00Z',
            validator_pat='x',
            miner_github_ids={'1001'},
            known_issues=set(),
            result=result,
            lookup_cap=10,
        )
    )

    assert lookups == 0, f'Expected 0 lookups charged, got {lookups}'
    assert len(result.get('1001', [])) == 3
