"""Tests for `_scan_repo` closed_at lookback window enforcement.

GitHub REST `/repos/{repo}/issues?since=...` filters by `updated_at`, not
`closed_at`, so response pages can contain ancient issues that were merely
re-touched (comment, label, reaction) within the window. `_scan_repo` must
drop these client-side before spending solver-lookup budget on them.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from gittensor.validator.issue_discovery import repo_scan


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _iso(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _lookback_str(days: int = 35) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(days=days))


def _miner_issue(number: int, closed_at: str | None) -> dict:
    return {
        'number': number,
        'closed_at': closed_at,
        'title': f'issue {number}',
        'created_at': _iso(datetime.now(timezone.utc) - timedelta(days=60)),
        'user': {'id': 999, 'login': 'miner'},
    }


class TestScanRepoClosedAtWindow:
    def _call(self, fetched_issues, lookback_date=None):
        """Run `_scan_repo` with mocked dependencies and return the collected result."""
        lookback_date = lookback_date or _lookback_str()
        result: dict = {}

        with patch.object(
            repo_scan, '_fetch_closed_issues', return_value=fetched_issues
        ), patch.object(
            repo_scan,
            'find_solver_from_cross_references',
            return_value=(None, None),
        ):
            _run(
                repo_scan._scan_repo(
                    repo_name='owner/repo',
                    lookback_date=lookback_date,
                    validator_pat='fake-token',
                    miner_github_ids={'999'},
                    known_issues=set(),
                    result=result,
                    lookup_cap=10,
                )
            )
        return result

    def test_drops_issue_closed_before_window(self):
        now = datetime.now(timezone.utc)
        fetched = [
            _miner_issue(1, _iso(now - timedelta(days=5))),
            _miner_issue(2, '2023-01-01T00:00:00Z'),
        ]
        result = self._call(fetched)
        numbers = [i.number for i in result.get('999', [])]
        assert numbers == [1]

    def test_drops_issue_with_null_closed_at(self):
        now = datetime.now(timezone.utc)
        fetched = [
            _miner_issue(3, None),
            _miner_issue(4, _iso(now - timedelta(days=1))),
        ]
        result = self._call(fetched)
        numbers = [i.number for i in result.get('999', [])]
        assert numbers == [4]

    def test_keeps_all_when_all_in_window(self):
        now = datetime.now(timezone.utc)
        fetched = [
            _miner_issue(n, _iso(now - timedelta(days=n)))
            for n in (1, 10, 30)
        ]
        result = self._call(fetched)
        numbers = sorted(i.number for i in result.get('999', []))
        assert numbers == [1, 10, 30]

    def test_boundary_at_since_is_kept(self):
        lookback = _lookback_str()
        fetched = [_miner_issue(7, lookback)]
        result = self._call(fetched, lookback_date=lookback)
        numbers = [i.number for i in result.get('999', [])]
        assert numbers == [7]

    def test_unparseable_lookback_does_not_filter(self):
        # Safety net: if lookback_date can't be parsed, fall back to the
        # pre-fix behavior rather than silently wiping the scan.
        now = datetime.now(timezone.utc)
        fetched = [_miner_issue(99, _iso(now - timedelta(days=1000)))]
        result = self._call(fetched, lookback_date='not-a-date')
        numbers = [i.number for i in result.get('999', [])]
        assert numbers == [99]
