# Entrius 2025

"""Broadcast PredictionSynapse from miner to all validator axons."""

import asyncio
import os

import bittensor as bt

from gittensor.synapses import PredictionSynapse


def broadcast_predictions(
    payload: dict[str, object],
    wallet_name: str,
    wallet_hotkey: str,
    ws_endpoint: str,
) -> dict[str, object]:
    """Broadcast PredictionSynapse to all validator axons via dendrite.

    Args:
        payload: Dict with issue_id, repository, predictions.
        wallet_name: Bittensor wallet name.
        wallet_hotkey: Bittensor hotkey name.
        ws_endpoint: Subtensor RPC endpoint.

    Returns:
        Dict with success, total_validators, accepted, rejected, results.
    """
    github_pat = os.getenv('GITTENSOR_MINER_PAT')
    if not github_pat:
        return {'success': False, 'error': 'GITTENSOR_MINER_PAT environment variable not set.', 'results': []}

    wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
    subtensor = bt.Subtensor(network=ws_endpoint)
    metagraph = subtensor.metagraph(netuid=subtensor.get_subnets()[0] if hasattr(subtensor, 'get_subnets') else 74)
    dendrite = bt.Dendrite(wallet=wallet)

    synapse = PredictionSynapse(
        github_access_token=github_pat,
        issue_id=int(payload['issue_id']),
        repository=str(payload['repository']),
        predictions={int(k): float(v) for k, v in payload['predictions'].items()},
    )

    # Get all validator axons (neurons with stake that serve axons).
    validator_axons = [
        axon for uid, axon in enumerate(metagraph.axons)
        if metagraph.S[uid] > 0 and axon.ip != '0.0.0.0'
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
        results.append({
            'validator': axon.hotkey[:16],
            'accepted': resp.accepted if hasattr(resp, 'accepted') else None,
            'rejection_reason': resp.rejection_reason if hasattr(resp, 'rejection_reason') else None,
            'status_code': resp.dendrite.status_code if hasattr(resp, 'dendrite') else None,
        })

    accepted_count = sum(1 for r in results if r['accepted'] is True)
    return {
        'success': accepted_count > 0,
        'total_validators': len(validator_axons),
        'accepted': accepted_count,
        'rejected': len(results) - accepted_count,
        'results': results,
    }
