# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Tests for mirror tracked-repo reconciliation."""

from unittest.mock import MagicMock

import pytest

from gittensor.validator.utils.config import MIRROR_DEREG_SNAPSHOTS, MIRROR_MAX_TRACKED_REPOS
from gittensor.validator.weight_consensus import mirror_sync
from gittensor.validator.weight_consensus.mirror_sync import sync_mirror_repos


@pytest.fixture
def admin(monkeypatch):
    """Enable sync and capture admin API traffic."""
    monkeypatch.setattr(mirror_sync, 'MIRROR_ADMIN_API_KEY', 'key')
    calls = {'posts': []}

    def fake_get(url, **kwargs):
        response = MagicMock()
        response.json.return_value = calls['registry']
        return response

    def fake_post(url, json=None, **kwargs):
        calls['posts'].append((url.split('/api/v1/admin')[1], json))
        return MagicMock()

    monkeypatch.setattr(mirror_sync.requests, 'get', fake_get)
    monkeypatch.setattr(mirror_sync.requests, 'post', fake_post)
    return calls


def _db(recent_snapshots, recently_voted):
    """Fake psycopg connection serving the two hysteresis queries."""
    cursor = MagicMock()
    cursor.__enter__ = lambda self: cursor
    cursor.__exit__ = MagicMock(return_value=False)
    results = [[(b,) for b in recent_snapshots], [(r,) for r in recently_voted]]
    cursor.fetchall.side_effect = results
    connection = MagicMock()
    connection.cursor.return_value = cursor
    return connection


def test_disabled_without_api_key(monkeypatch):
    monkeypatch.setattr(mirror_sync, 'MIRROR_ADMIN_API_KEY', '')
    get = MagicMock()
    monkeypatch.setattr(mirror_sync.requests, 'get', get)
    sync_mirror_repos(MagicMock(), 3600, {'a/b'}, {'a/b': 1.0})
    get.assert_not_called()


def test_registers_unregistered_voted_repos_with_backfill(admin):
    admin['registry'] = [
        {'repoFullName': 'a/b', 'registered': False, 'hasInstallation': True},
        {'repoFullName': 'c/d', 'registered': True, 'hasInstallation': True},
    ]
    sync_mirror_repos(_db([], []), 3600, {'a/b', 'c/d'}, {'a/b': 0.5, 'c/d': 0.5})
    assert ('/repos/register', {'repoFullName': 'a/b'}) in admin['posts']
    assert any(path == '/backfill' and body['repoFullName'] == 'a/b' for path, body in admin['posts'])
    assert not any(body.get('repoFullName') == 'c/d' for _, body in admin['posts'])


def test_pending_app_install_only_warns(admin):
    admin['registry'] = []
    sync_mirror_repos(_db([], []), 3600, {'new/repo'}, {'new/repo': 1.0})
    assert admin['posts'] == []


def test_deregisters_after_full_absence_window(admin):
    admin['registry'] = [{'repoFullName': 'old/repo', 'registered': True, 'hasInstallation': True}]
    db = _db(recent_snapshots=list(range(MIRROR_DEREG_SNAPSHOTS)), recently_voted=['a/b'])
    sync_mirror_repos(db, 3600, {'a/b'}, {'a/b': 1.0})
    assert ('/repos/deregister', {'repoFullName': 'old/repo'}) in admin['posts']


def test_no_dereg_with_short_history_or_recent_vote_or_inactive_gate(admin):
    registry = [{'repoFullName': 'old/repo', 'registered': True, 'hasInstallation': True}]

    admin['registry'] = registry
    sync_mirror_repos(_db([1], []), 3600, {'a/b'}, {'a/b': 1.0})  # short history
    assert not any(path == '/repos/deregister' for path, _ in admin['posts'])

    admin['registry'] = registry
    sync_mirror_repos(_db(list(range(MIRROR_DEREG_SNAPSHOTS)), ['old/repo']), 3600, {'a/b'}, {'a/b': 1.0})
    assert not any(path == '/repos/deregister' for path, _ in admin['posts'])

    admin['registry'] = registry
    sync_mirror_repos(_db(list(range(MIRROR_DEREG_SNAPSHOTS)), []), 3600, {'a/b'}, None)  # gate inactive
    assert not any(path == '/repos/deregister' for path, _ in admin['posts'])


def test_cap_registers_top_by_aggregate_share(admin):
    repos = {f'o/r{i}': i + 1 for i in range(MIRROR_MAX_TRACKED_REPOS + 5)}
    shares = {name: weight / sum(repos.values()) for name, weight in repos.items()}
    admin['registry'] = [{'repoFullName': name, 'registered': False, 'hasInstallation': True} for name in repos]
    sync_mirror_repos(_db([], []), 3600, set(repos), shares)
    registered = [body['repoFullName'] for path, body in admin['posts'] if path == '/repos/register']
    assert len(registered) == MIRROR_MAX_TRACKED_REPOS
    assert f'o/r{MIRROR_MAX_TRACKED_REPOS + 4}' in registered  # highest share kept
    assert 'o/r0' not in registered  # lowest share dropped
