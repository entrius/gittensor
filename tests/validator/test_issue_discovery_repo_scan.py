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
