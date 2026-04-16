#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression test for issue #451 — closed_at client-side filter."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from unittest.mock import patch

from gittensor.classes import Issue
from gittensor.validator.issue_discovery.repo_scan import _scan_repo

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
    return asyncio.get_event_loop().run_until_complete(coro)


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
