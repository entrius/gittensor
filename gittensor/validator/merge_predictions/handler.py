# Entrius 2025

"""Axon handler for PredictionSynapse.

Attached to the validator's axon via functools.partial in Validator.__init__().
Runs in the axon's FastAPI thread pool — fully parallel to the main scoring loop.
"""

from typing import TYPE_CHECKING, Tuple

import bittensor as bt

from gittensor.synapses import PredictionSynapse
from gittensor.validator.utils.github_validation import validate_github_credentials
from gittensor.validator.merge_predictions.checks import check_issue_active, check_prs_open
from gittensor.validator.merge_predictions.validation import validate_prediction_values

if TYPE_CHECKING:
    from neurons.validator import Validator


async def handle_prediction(validator: 'Validator', synapse: PredictionSynapse) -> PredictionSynapse:
    """Validate and store a miner's prediction. Runs in axon thread pool."""

    mp_storage = validator.mp_storage
    miner_hotkey = synapse.dendrite.hotkey
    uid = validator.metagraph.hotkeys.index(miner_hotkey)

    # 1) Verify issue is active on-chain
    error = check_issue_active(validator, synapse.issue_id)
    if error:
        synapse.accepted = False
        synapse.rejection_reason = error
        return synapse

    # 2) Verify predicted PRs are still open on GitHub
    error = check_prs_open(synapse.repository, synapse.issue_id, synapse.predictions)
    if error:
        synapse.accepted = False
        synapse.rejection_reason = error
        return synapse

    # 3) Validate GitHub identity + account age
    github_id, error = validate_github_credentials(uid, synapse.github_access_token)
    if error:
        synapse.accepted = False
        synapse.rejection_reason = error
        return synapse

    # 4) Validate prediction values
    error = validate_prediction_values(synapse.predictions)
    if error:
        synapse.accepted = False
        synapse.rejection_reason = error
        return synapse

    # 5) Per-PR: check cooldown, check total <= 1.0, store
    stored_prs = []
    for pr_number, pred_value in synapse.predictions.items():
        # Cooldown check
        cooldown_remaining = mp_storage.check_cooldown(uid, miner_hotkey, synapse.issue_id, pr_number)
        if cooldown_remaining is not None:
            synapse.accepted = False
            synapse.rejection_reason = f'PR #{pr_number} on cooldown ({cooldown_remaining:.0f}s remaining)'
            return synapse

        # Total probability check (existing + new, excluding this PR if it's an update)
        existing_total = mp_storage.get_miner_total_for_issue(
            uid, miner_hotkey, synapse.issue_id, exclude_pr=pr_number
        )
        if existing_total + pred_value > 1.0:
            synapse.accepted = False
            synapse.rejection_reason = (
                f'Total probability would exceed 1.0 '
                f'(existing: {existing_total:.4f} + new: {pred_value:.4f} = {existing_total + pred_value:.4f})'
            )
            return synapse

        stored_prs.append((pr_number, pred_value))

    # 6) Compute variance at time of submission and store all predictions
    variance = mp_storage.compute_current_variance(synapse.issue_id)

    for pr_number, pred_value in stored_prs:
        mp_storage.store_prediction(
            uid=uid,
            hotkey=miner_hotkey,
            github_id=github_id,
            issue_id=synapse.issue_id,
            repository=synapse.repository,
            pr_number=pr_number,
            prediction=pred_value,
            variance_at_prediction=variance,
        )

    bt.logging.info(
        f'Prediction stored: uid={uid}, issue={synapse.issue_id}, '
        f'PRs={[pr for pr, _ in stored_prs]}, github_id={github_id}'
    )

    synapse.accepted = True
    return synapse


async def blacklist_prediction(validator: 'Validator', synapse: PredictionSynapse) -> Tuple[bool, str]:
    """Reject synapses from unregistered hotkeys."""
    if synapse.dendrite is None or synapse.dendrite.hotkey is None:
        return True, 'Missing dendrite or hotkey'

    if synapse.dendrite.hotkey not in validator.metagraph.hotkeys:
        return True, 'Unregistered hotkey'

    return False, 'Hotkey recognized'


async def priority_prediction(validator: 'Validator', synapse: PredictionSynapse) -> float:
    """Priority by stake — higher stake = processed first."""
    if synapse.dendrite is None or synapse.dendrite.hotkey is None:
        return 0.0

    try:
        uid = validator.metagraph.hotkeys.index(synapse.dendrite.hotkey)
        return float(validator.metagraph.S[uid])
    except ValueError:
        return 0.0
