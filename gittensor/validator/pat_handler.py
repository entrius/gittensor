# Entrius 2025

"""Axon handlers for miner PAT broadcasting and checking.

Miners push their GitHub PAT to validators via PatBroadcastSynapse.
Miners check if a validator has their PAT via PatCheckSynapse.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Tuple

import bittensor as bt
import requests

from gittensor.constants import BASE_GITHUB_API_URL
from gittensor.synapses import PatBroadcastSynapse, PatCheckSynapse
from gittensor.validator import pat_storage
from gittensor.validator.utils.github_validation import validate_github_credentials

if TYPE_CHECKING:
    from neurons.validator import Validator


# ---------------------------------------------------------------------------
# PatBroadcastSynapse handlers
# ---------------------------------------------------------------------------

async def handle_pat_broadcast(validator: Validator, synapse: PatBroadcastSynapse) -> PatBroadcastSynapse:
    """Validate and store a miner's GitHub PAT."""
    hotkey = synapse.dendrite.hotkey

    def _reject(reason: str) -> PatBroadcastSynapse:
        synapse.accepted = False
        synapse.rejection_reason = reason
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

    # 3. Test query against a known repo to catch org-restricted PATs
    test_error = _test_pat_against_repo(synapse.github_access_token)
    if test_error:
        return _reject(f'PAT test query failed: {test_error}')

    # 4. Store PAT
    pat_storage.save_pat(hotkey=hotkey, uid=uid, pat=synapse.github_access_token, github_id=github_id)

    synapse.accepted = True
    bt.logging.success(f'PAT broadcast accepted — UID: {uid}, hotkey: {hotkey[:16]}..., github_id: {github_id}')
    return synapse


async def blacklist_pat_broadcast(validator: Validator, synapse: PatBroadcastSynapse) -> Tuple[bool, str]:
    """Reject PAT broadcasts from unregistered hotkeys."""
    hotkey = synapse.dendrite.hotkey
    if hotkey not in validator.metagraph.hotkeys:
        return True, f'Hotkey {hotkey[:16]}... not registered'
    return False, 'Hotkey recognized'


async def priority_pat_broadcast(validator: Validator, synapse: PatBroadcastSynapse) -> float:
    """Prioritize PAT broadcasts by stake."""
    hotkey = synapse.dendrite.hotkey
    if hotkey not in validator.metagraph.hotkeys:
        return 0.0
    uid = validator.metagraph.hotkeys.index(hotkey)
    return float(validator.metagraph.S[uid])


# ---------------------------------------------------------------------------
# PatCheckSynapse handlers
# ---------------------------------------------------------------------------

async def handle_pat_check(validator: Validator, synapse: PatCheckSynapse) -> PatCheckSynapse:
    """Check if the validator has the miner's PAT stored."""
    hotkey = synapse.dendrite.hotkey
    entry = pat_storage.get_pat_by_hotkey(hotkey)
    synapse.has_pat = entry is not None
    return synapse


async def blacklist_pat_check(validator: Validator, synapse: PatCheckSynapse) -> Tuple[bool, str]:
    """Reject PAT checks from unregistered hotkeys."""
    hotkey = synapse.dendrite.hotkey
    if hotkey not in validator.metagraph.hotkeys:
        return True, f'Hotkey {hotkey[:16]}... not registered'
    return False, 'Hotkey recognized'


async def priority_pat_check(validator: Validator, synapse: PatCheckSynapse) -> float:
    """Prioritize PAT checks by stake."""
    hotkey = synapse.dendrite.hotkey
    if hotkey not in validator.metagraph.hotkeys:
        return 0.0
    uid = validator.metagraph.hotkeys.index(hotkey)
    return float(validator.metagraph.S[uid])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# A known public repo used for test queries. The PAT just needs read access to public repos.
_TEST_REPO = 'torvalds/linux'


def _test_pat_against_repo(pat: str) -> str | None:
    """Run a test API call against a known repo to catch org-restricted or expired PATs.

    Returns an error string on failure, None on success.
    """
    headers = {'Authorization': f'token {pat}', 'Accept': 'application/vnd.github.v3+json'}
    try:
        response = requests.get(
            f'{BASE_GITHUB_API_URL}/repos/{_TEST_REPO}/pulls?state=closed&per_page=1',
            headers=headers,
            timeout=15,
        )
        if response.status_code == 200:
            return None
        return f'GitHub API returned {response.status_code}'
    except requests.RequestException as e:
        return str(e)
