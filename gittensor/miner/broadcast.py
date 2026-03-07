# Entrius 2025

"""Broadcast PredictionSynapse from miner to all validator axons."""

import asyncio

import bittensor as bt

from gittensor.miner.token_mgmt import load_token
from gittensor.synapses import PredictionSynapse


def broadcast_predictions(
    payload: dict[str, object],
    wallet_name: str,
    wallet_hotkey: str,
    ws_endpoint: str,
    netuid: int,
) -> dict[str, object]:
    """Broadcast PredictionSynapse to all validator axons via dendrite.

    Args:
        payload: Dict with issue_id, repository, predictions.
        wallet_name: Bittensor wallet name.
        wallet_hotkey: Bittensor hotkey name.
        ws_endpoint: Subtensor RPC endpoint.
        netuid: Subnet UID to broadcast on.

    Returns:
        Dict with success, total_validators, accepted, rejected, results.
    """
    github_pat = load_token(quiet=True)
    if not github_pat:
        return {'success': False, 'error': 'GITTENSOR_MINER_PAT not set or invalid.', 'results': []}

    wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
    subtensor = bt.Subtensor(network=ws_endpoint)
    metagraph = subtensor.metagraph(netuid=netuid)
    dendrite = bt.Dendrite(wallet=wallet)

    synapse = PredictionSynapse(
        github_access_token=github_pat,
        issue_id=int(payload['issue_id']),
        repository=str(payload['repository']),
        predictions={int(k): float(v) for k, v in payload['predictions'].items()},
    )

    # Get axons for high-trust validators (> 0.6 vtrust) with permit that are actively serving.
    validator_axons = [
        axon
        for uid, axon in enumerate(metagraph.axons)
        if metagraph.validator_permit[uid] and axon.is_serving and float(metagraph.Tv[uid]) > 0.6
    ]

    if not validator_axons:
        return {'success': False, 'error': 'No reachable validator axons found on the network.', 'results': []}

    responses = asyncio.get_event_loop().run_until_complete(
        dendrite(
            axons=validator_axons,
            synapse=synapse,
            deserialize=False,
            timeout=12.0,
        )
    )

    results = []
    for axon, resp in zip(validator_axons, responses):
        results.append(
            {
                'validator': axon.hotkey[:16],
                'accepted': resp.accepted if hasattr(resp, 'accepted') else None,
                'rejection_reason': resp.rejection_reason if hasattr(resp, 'rejection_reason') else None,
                'status_code': resp.dendrite.status_code if hasattr(resp, 'dendrite') else None,
            }
        )

    accepted_count = sum(1 for r in results if r['accepted'] is True)
    return {
        'success': accepted_count > 0,
        'total_validators': len(validator_axons),
        'accepted': accepted_count,
        'rejected': len(results) - accepted_count,
        'results': results,
    }
