# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for repo_scan: is_transferred population from REST state_reason."""

import asyncio

from gittensor.validator.issue_discovery import repo_scan


def _make_raw(state_reason: str, number: int) -> dict:
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
        repo_scan, '_fetch_closed_issues',
        lambda repo_name, since, token: raw_issues,
    )
    monkeypatch.setattr(
        repo_scan, 'find_solver_from_cross_references',
        lambda repo, issue_number, token: (None, None),
    )

    result: dict = {}
    asyncio.run(repo_scan._scan_repo(
        repo_name='test/repo',
        lookback_date='2026-01-01',
        validator_pat='x',
        miner_github_ids={'1001'},
        known_issues=set(),
        result=result,
        lookup_cap=10,
    ))
    return result


def test_scan_repo_sets_is_transferred_for_transferred_issue(monkeypatch):
    result = _run_scan(monkeypatch, [_make_raw('transferred', 42)])

    assert '1001' in result
    assert result['1001'][0].is_transferred is True


def test_scan_repo_is_transferred_false_for_completed_issue(monkeypatch):
    result = _run_scan(monkeypatch, [_make_raw('completed', 43)])

    assert '1001' in result
    assert result['1001'][0].is_transferred is False
