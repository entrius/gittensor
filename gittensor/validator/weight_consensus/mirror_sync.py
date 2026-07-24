# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Reconciles the GT mirror's tracked-repo set with the consensus snapshot.

Runs inside the team validator only (enabled by MIRROR_ADMIN_API_KEY) after
each snapshot is stored. The tracked target is the union of eligible voters'
baskets — bounded by 10 x voters, changing at most 2x/day — so the mirror sees
a stable, definitive list: registrations happen on first appearance,
deregistrations only after a repo is absent for a full hysteresis window.
"""

from typing import Dict, List, Optional, Set

import bittensor as bt
import requests

from gittensor.constants import GITTENSOR_MIRROR_DEFAULT_URL
from gittensor.validator.utils.config import (
    MIRROR_ADMIN_API_KEY,
    MIRROR_BACKFILL_DAYS,
    MIRROR_DEREG_SNAPSHOTS,
    MIRROR_MAX_TRACKED_REPOS,
)

_TIMEOUT = 30


def sync_mirror_repos(
    db_connection,
    snapshot_block: int,
    voted_repos: Set[str],
    aggregate_shares: Optional[Dict[str, float]],
) -> None:
    """Diff the voted-repo union against the mirror's registry and reconcile.

    Registers (+deep backfill) voted repos whose GitHub App row exists, warns
    on repos pending App install, and deregisters repos absent from the last
    MIRROR_DEREG_SNAPSHOTS snapshots — only while the consensus gate is active,
    so pre-consensus tracking is never torn down during rollout.
    """
    if not MIRROR_ADMIN_API_KEY:
        return

    if len(voted_repos) > MIRROR_MAX_TRACKED_REPOS:
        shares = aggregate_shares or {}
        kept = sorted(voted_repos, key=lambda r: (-shares.get(r, 0.0), r))[:MIRROR_MAX_TRACKED_REPOS]
        bt.logging.warning(
            f'mirror_sync: {len(voted_repos)} voted repos exceed cap {MIRROR_MAX_TRACKED_REPOS}; '
            f'registering top by aggregate share'
        )
        voted_repos = set(kept)

    registry = {entry['repoFullName'].lower(): entry for entry in _admin_get('/api/v1/admin/repos')}

    for repo in sorted(voted_repos):
        entry = registry.get(repo)
        if entry is None:
            bt.logging.warning(f'mirror_sync: {repo} is voted but pending GitHub App install — cannot track yet')
        elif not entry['registered']:
            _register(repo)

    if aggregate_shares is None:
        return  # gate inactive — consensus does not govern the list yet

    stale_candidates = [
        entry['repoFullName']
        for entry in registry.values()
        if entry['registered'] and entry['repoFullName'].lower() not in voted_repos
    ]
    for repo in sorted(_absent_for_window(db_connection, stale_candidates)):
        _deregister(repo)


def _absent_for_window(db_connection, candidates: List[str]) -> List[str]:
    """Candidates absent from every basket in the last MIRROR_DEREG_SNAPSHOTS
    snapshots. Requires a full window of history so a fresh deployment never
    mass-deregisters."""
    if not candidates:
        return []
    with db_connection.cursor() as cur:
        cur.execute(
            'SELECT DISTINCT snapshot_block FROM validator_weight_baskets ORDER BY snapshot_block DESC LIMIT %s',
            (MIRROR_DEREG_SNAPSHOTS,),
        )
        recent = [row[0] for row in cur.fetchall()]
        if len(recent) < MIRROR_DEREG_SNAPSHOTS:
            return []
        cur.execute(
            'SELECT DISTINCT jsonb_object_keys(basket) FROM validator_weight_baskets WHERE snapshot_block = ANY(%s)',
            (recent,),
        )
        recently_voted = {row[0] for row in cur.fetchall()}
    return [repo for repo in candidates if repo.lower() not in recently_voted]


def _register(repo: str) -> None:
    _admin_post('/api/v1/admin/repos/register', {'repoFullName': repo})
    _admin_post('/api/v1/admin/backfill', {'repoFullName': repo, 'days': MIRROR_BACKFILL_DAYS})
    bt.logging.info(f'mirror_sync: registered {repo} (+{MIRROR_BACKFILL_DAYS}d backfill)')


def _deregister(repo: str) -> None:
    _admin_post('/api/v1/admin/repos/deregister', {'repoFullName': repo})
    bt.logging.info(f'mirror_sync: deregistered {repo} (absent {MIRROR_DEREG_SNAPSHOTS} snapshots)')


def _admin_get(path: str) -> list:
    response = requests.get(
        f'{GITTENSOR_MIRROR_DEFAULT_URL}{path}', headers={'x-api-key': MIRROR_ADMIN_API_KEY}, timeout=_TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def _admin_post(path: str, body: dict) -> None:
    response = requests.post(
        f'{GITTENSOR_MIRROR_DEFAULT_URL}{path}',
        json=body,
        headers={'x-api-key': MIRROR_ADMIN_API_KEY},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
