# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Raw chain access for commitment payloads.

The SDK's convenience readers (``get_all_commitments`` / ``decode_metadata``)
decode fields as utf-8 with ``errors='ignore'`` and silently corrupt binary
payloads, so reads go through ``query_map``/``query`` and the bytes are
extracted here. Writes use the low-level pallet builder because the high-level
``set_commitment`` only supports Raw0-128 strings.
"""

from typing import Any, Dict, List, Optional, cast

import bittensor as bt

from gittensor.validator.weight_consensus.codec import decode_prefs


def _to_bytes(value: Any) -> Optional[bytes]:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str) and value.startswith('0x'):
        try:
            return bytes.fromhex(value[2:])
        except ValueError:
            return None
    if isinstance(value, (list, tuple)) and all(isinstance(b, int) and 0 <= b <= 255 for b in value):
        return bytes(value)
    return None


def extract_payload_candidates(commitment_value: Any) -> List[bytes]:
    """Collect every Raw/BigRaw field's bytes from a decoded CommitmentOf value,
    tolerating the varying nesting SCALE decoding produces."""
    candidates: List[bytes] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and (key == 'BigRaw' or key.startswith('Raw')):
                    payload = _to_bytes(value)
                    if payload is not None:
                        candidates.append(payload)
                else:
                    walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(getattr(commitment_value, 'value', commitment_value))
    return candidates


def extract_prefs(commitment_value: Any) -> Optional[bytes]:
    """Return the first field payload that decodes as a valid preference vector."""
    return next((c for c in extract_payload_candidates(commitment_value) if decode_prefs(c) is not None), None)


def fetch_all_commitments(subtensor: 'bt.Subtensor', netuid: int, block: int) -> Dict[str, bytes]:
    """All hotkeys' valid preference payloads at a block: hotkey -> payload."""
    records = subtensor.query_map(module='Commitments', name='CommitmentOf', params=[netuid], block=block)
    commitments: Dict[str, bytes] = {}
    for hotkey, value in cast(Any, records) or []:
        payload = extract_prefs(value)
        if payload is not None:
            commitments[str(getattr(hotkey, 'value', hotkey))] = payload
    return commitments


def fetch_own_prefs(subtensor: 'bt.Subtensor', netuid: int, hotkey_ss58: str) -> Optional[Dict[str, int]]:
    """The validator's currently published preference vector, if any."""
    value = subtensor.substrate.query(
        module='Commitments', storage_function='CommitmentOf', params=[netuid, hotkey_ss58]
    )
    payload = extract_prefs(value)
    return decode_prefs(payload) if payload is not None else None


def publish_payload(subtensor: 'bt.Subtensor', wallet: 'bt.Wallet', netuid: int, payload: bytes) -> bool:
    """Publish one BigRaw commitment field signed by the validator hotkey."""
    # Imported lazily so the CLI's --help/completion stub of `bittensor` never
    # has to resolve SDK submodules.
    from bittensor.core.extrinsics.pallets.commitments import Commitments

    # The pallet builder is typed for sync and async subtensors; sync returns the call directly.
    call = cast(Any, Commitments(subtensor).set_commitment(netuid=netuid, info={'fields': [[{'BigRaw': payload}]]}))
    response = subtensor.sign_and_send_extrinsic(
        call=call, wallet=wallet, sign_with='hotkey', wait_for_inclusion=True, wait_for_finalization=False
    )
    return bool(response.success)
