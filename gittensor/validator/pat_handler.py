# Entrius 2025

"""Axon handlers for miner PAT broadcasting and checking.

Miners push their GitHub PAT to validators via PatBroadcastSynapse.
Miners check if a validator has their PAT via PatCheckSynapse.
"""

from typing import TYPE_CHECKING, Optional, Tuple

import bittensor as bt
import requests

from gittensor.constants import BASE_GITHUB_API_URL
from gittensor.synapses import PatBroadcastSynapse, PatCheckSynapse
from gittensor.validator import pat_storage
from gittensor.validator.utils.github_validation import validate_github_credentials

if TYPE_CHECKING:
    from neurons.validator import Validator


def _get_hotkey(synapse: bt.Synapse) -> str:
    """Extract the caller's hotkey from a synapse, raising if missing."""
    assert synapse.dendrite is not None and synapse.dendrite.hotkey is not None
    return synapse.dendrite.hotkey


# ---------------------------------------------------------------------------
# PatBroadcastSynapse handlers
# ---------------------------------------------------------------------------


async def handle_pat_broadcast(validator: 'Validator', synapse: PatBroadcastSynapse) -> PatBroadcastSynapse:
    """Validate and store a miner's GitHub PAT."""
    hotkey = _get_hotkey(synapse)

    def _reject(reason: str) -> PatBroadcastSynapse:
        synapse.accepted = False
        synapse.rejection_reason = reason
        synapse.github_access_token = ''
        bt.logging.warning(f'PAT broadcast rejected — hotkey: {hotkey[:16]}... reason: {reason}')
        return synapse

    # 1. Verify hotkey is registered on the subnet
    if hotkey not in validator.metagraph.hotkeys:
        return _reject('Hotkey not registered on subnet')

    uid = validator.metagraph.hotkeys.index(hotkey)

    # 2. Validate PAT (checks it works, extracts github_id, verifies account age)
    github_id, error = validate_github_credentials(uid, synapse.github_access_token)
    if error:
        return _reject(error)

    # 3. Enforce GitHub identity pinning — same hotkey cannot switch GitHub accounts
    existing = pat_storage.get_pat_by_uid(uid)
    if existing and existing.get('hotkey') == hotkey and existing.get('github_id'):
        if existing['github_id'] != github_id:
            return _reject(
                'GitHub identity is locked for this hotkey. Deregister and re-register to change GitHub accounts.'
            )

    # 4. Test query against a known repo to catch org-restricted PATs
    test_error = _test_pat_against_repo(synapse.github_access_token)
    if test_error:
        return _reject(f'PAT test query failed: {test_error}')

    # 5. Store PAT
    assert github_id is not None  # guaranteed by validate_github_credentials success
    pat_storage.save_pat(uid=uid, hotkey=hotkey, pat=synapse.github_access_token, github_id=github_id)

    # Clear PAT from response so it isn't echoed back
    synapse.github_access_token = ''
    synapse.accepted = True
    bt.logging.success(f'PAT broadcast accepted — UID: {uid}, hotkey: {hotkey[:16]}..., github_id: {github_id}')
    return synapse


async def blacklist_pat_broadcast(validator: 'Validator', synapse: PatBroadcastSynapse) -> Tuple[bool, str]:
    """Reject PAT broadcasts from unregistered hotkeys."""
    hotkey = _get_hotkey(synapse)
    if hotkey not in validator.metagraph.hotkeys:
        return True, f'Hotkey {hotkey[:16]}... not registered'
    return False, 'Hotkey recognized'


async def priority_pat_broadcast(validator: 'Validator', synapse: PatBroadcastSynapse) -> float:
    """Prioritize PAT broadcasts by stake."""
    hotkey = _get_hotkey(synapse)
    if hotkey not in validator.metagraph.hotkeys:
        return 0.0
    uid = validator.metagraph.hotkeys.index(hotkey)
    return float(validator.metagraph.S[uid])


# ---------------------------------------------------------------------------
# PatCheckSynapse handlers
# ---------------------------------------------------------------------------


async def handle_pat_check(validator: 'Validator', synapse: PatCheckSynapse) -> PatCheckSynapse:
    """Check if the validator has the miner's PAT stored and re-validate it."""
    hotkey = _get_hotkey(synapse)
    uid = validator.metagraph.hotkeys.index(hotkey)
    entry = pat_storage.get_pat_by_uid(uid)

    bt.logging.info(f'PAT check request — UID: {uid}, hotkey: {hotkey[:16]}...')

    # Check if PAT exists and hotkey matches (not a stale entry from a previous miner)
    if entry is None or entry.get('hotkey') != hotkey:
        synapse.has_pat = False
        synapse.pat_valid = False
        synapse.rejection_reason = 'No PAT stored for this miner'
        bt.logging.info(f'PAT check result — UID: {uid}: no PAT stored')
        return synapse

    synapse.has_pat = True

    # Re-validate the stored PAT
    _, error = validate_github_credentials(uid, entry['pat'])
    if error:
        synapse.pat_valid = False
        synapse.rejection_reason = error
        bt.logging.warning(f'PAT check result — UID: {uid}: validation failed: {error}')
        return synapse

    test_error = _test_pat_against_repo(entry['pat'])
    if test_error:
        synapse.pat_valid = False
        synapse.rejection_reason = f'PAT test query failed: {test_error}'
        bt.logging.warning(f'PAT check result — UID: {uid}: test query failed: {test_error}')
        return synapse

    synapse.pat_valid = True
    bt.logging.success(f'PAT check result — UID: {uid}: valid')
    return synapse


async def blacklist_pat_check(validator: 'Validator', synapse: PatCheckSynapse) -> Tuple[bool, str]:
    """Reject PAT checks from unregistered hotkeys."""
    hotkey = _get_hotkey(synapse)
    if hotkey not in validator.metagraph.hotkeys:
        return True, f'Hotkey {hotkey[:16]}... not registered'
    return False, 'Hotkey recognized'


async def priority_pat_check(validator: 'Validator', synapse: PatCheckSynapse) -> float:
    """Prioritize PAT checks by stake."""
    hotkey = _get_hotkey(synapse)
    if hotkey not in validator.metagraph.hotkeys:
        return 0.0
    uid = validator.metagraph.hotkeys.index(hotkey)
    return float(validator.metagraph.S[uid])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_TEST_GRAPHQL_QUERY = '{ viewer { login } }'


def _test_pat_against_repo(pat: str) -> Optional[str]:
    """Run a test GraphQL call to verify the PAT has the access scoring requires.

    Scoring uses the GraphQL API to fetch miner PRs, so this mirrors the real path.
    Returns an error string on failure, None on success.
    """
    headers = {'Authorization': f'bearer {pat}', 'Accept': 'application/json'}
    try:
        response = requests.post(
            f'{BASE_GITHUB_API_URL}/graphql',
            json={'query': _TEST_GRAPHQL_QUERY},
            headers=headers,
            timeout=15,
        )
        if response.status_code != 200:
            return f'GitHub GraphQL API returned {response.status_code}'
        data = response.json()
        if 'errors' in data:
            return f'GraphQL error: {data["errors"][0].get("message", "unknown")}'
        return None
    except requests.RequestException as e:
        return str(e)
