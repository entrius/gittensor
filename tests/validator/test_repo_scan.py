#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression test for issue #451 — closed_at client-side filter."""

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from unittest.mock import patch

import requests

from gittensor.classes import Issue, MinerEvaluation
from gittensor.validator.issue_discovery import repo_scan
from gittensor.validator.issue_discovery.repo_scan import (
    _find_solver_in_worker,
    _scan_repo,
    scan_closed_issues,
)
from gittensor.validator.utils.load_weights import RepositoryConfig

MINER_GH_ID = '12345'
REPO = 'owner/repo'
TOKEN = 'ghp_test'


def _iso(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _make_issue(number: int, author_id: str, closed_at: Optional[str], updated_at: Optional[str] = None) -> dict:
    issue: dict = {
        'number': number,
        'title': f'Test issue #{number}',
        'user': {'id': int(author_id), 'login': 'miner'},
        'state': 'closed',
        'created_at': '2025-01-01T00:00:00Z',
    }
    if closed_at is not None:
        issue['closed_at'] = closed_at
    if updated_at is not None:
        issue['updated_at'] = updated_at
    return issue


def _run(coro):
    return asyncio.run(coro)


def _run_scan(issues: List[dict], lookback_date: str) -> Dict[str, List[Issue]]:
    result: Dict[str, List[Issue]] = {}
    with (
        patch('gittensor.validator.issue_discovery.repo_scan._fetch_closed_issues', return_value=issues),
        patch(
            'gittensor.validator.issue_discovery.repo_scan.find_solver_from_cross_references', return_value=(None, None)
        ),
    ):
        _run(_scan_repo(REPO, lookback_date, TOKEN, {MINER_GH_ID}, set(), result, 100))
    return result


def test_stale_issue_filtered_recent_passes():
    """Stale closed_at is dropped; recent closed_at passes through (regression #451)."""
    now = datetime.now(timezone.utc)
    lookback = _iso(now - timedelta(days=35))

    issues = [
        _make_issue(
            1, MINER_GH_ID, closed_at=_iso(now - timedelta(days=730)), updated_at=_iso(now - timedelta(days=1))
        ),
        _make_issue(2, MINER_GH_ID, closed_at=_iso(now - timedelta(days=3))),
    ]
    result = _run_scan(issues, lookback)

    assert MINER_GH_ID in result
    assert len(result[MINER_GH_ID]) == 1
    assert result[MINER_GH_ID][0].number == 2


def test_scan_closed_issues_pools_rest_session_and_uses_thread_local_for_solver():
    """REST path shares one scan-scoped Session; solver path uses per-thread Sessions."""
    now = datetime.now(timezone.utc)
    miner_evaluations = {0: MinerEvaluation(uid=0, hotkey='hk', github_id=MINER_GH_ID)}
    master_repositories = {REPO: RepositoryConfig(weight=1.0), 'owner/other': RepositoryConfig(weight=1.0)}

    issues = [_make_issue(1, MINER_GH_ID, closed_at=_iso(now - timedelta(days=1)))]

    with (
        patch('gittensor.validator.issue_discovery.repo_scan._fetch_closed_issues', return_value=issues) as mock_fetch,
        patch(
            'gittensor.validator.issue_discovery.repo_scan.find_solver_from_cross_references', return_value=(None, None)
        ) as mock_solver,
    ):
        asyncio.run(scan_closed_issues(miner_evaluations, master_repositories, TOKEN))

    fetch_sessions = {call.kwargs.get('session') for call in mock_fetch.call_args_list}
    solver_sessions = {call.kwargs.get('session') for call in mock_solver.call_args_list}

    assert mock_fetch.call_count == len(master_repositories)
    assert len(fetch_sessions) == 1
    rest_session = next(iter(fetch_sessions))
    assert isinstance(rest_session, requests.Session)

    assert mock_solver.call_count >= 1
    assert solver_sessions, 'Solver lookups must receive a Session'
    for solver_session in solver_sessions:
        assert isinstance(solver_session, requests.Session)
        assert solver_session is not rest_session


def test_find_solver_in_worker_uses_thread_local_session():
    """Same thread reuses one Session; different threads get distinct Sessions."""
    repo_scan._solver_session_local = threading.local()

    captured: List[requests.Session] = []

    def _capture(_repo, _issue, _token, *, session):
        captured.append(session)
        return (None, None)

    with patch('gittensor.validator.issue_discovery.repo_scan.find_solver_from_cross_references', side_effect=_capture):
        _find_solver_in_worker(REPO, 1, TOKEN)
        _find_solver_in_worker(REPO, 2, TOKEN)

        other: Dict[str, requests.Session] = {}

        def worker() -> None:
            _find_solver_in_worker(REPO, 3, TOKEN)
            other['session'] = captured[-1]

        t = threading.Thread(target=worker)
        t.start()
        t.join()

    assert captured[0] is captured[1], 'Same thread must reuse its Session'
    assert other['session'] is not captured[0], 'Different thread must get a distinct Session'
