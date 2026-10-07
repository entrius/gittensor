# The MIT License (MIT)
# Copyright © 2026 Entrius

"""A stand-in for Cathedral's side: a platform key derived from a public string and a receipt issued with it, in the
shape of the P0 run (``rd-guestquote-v0``: a TDX quote that is only well formed, no Intel signature). Tests only."""

import base64
import hashlib
import json

from Crypto.Signature import eddsa

from gittensor.challenges.attestation import Attestation, digest, public_hex
from gittensor.challenges.cathedral import CLAIMS_DOMAIN, image_measurement, receipt_bytes
from gittensor.challenges.head_to_head import canonical

SEED = hashlib.sha256(b'SAMPLE KEY - cathedral challenge receipt v1 - DO NOT TRUST').digest()
KEY = eddsa.import_private_key(SEED)
KEY_ID = 'fake-challenge-2026-10'
E2E_KEY = base64.b64encode(bytes(32)).decode()


def trusted_keys() -> dict:
    return {
        KEY_ID: {
            'algorithm': 'ed25519',
            'public_key_base64': base64.b64encode(bytes.fromhex(public_hex(KEY))).decode(),
            'status': 'active',
            'valid_from': '2026-10-01T00:00:00.000000Z',
            'valid_until': '2027-10-01T00:00:00.000000Z',
        }
    }


def quote(report_data: bytes) -> tuple[bytes, str]:
    """A TDX v4 quote layout holding ``report_data``, and its v2 image measurement."""
    header = (4).to_bytes(2, 'little') + (2).to_bytes(2, 'little') + (0x81).to_bytes(4, 'little') + bytes(40)
    registers = {name: hashlib.sha384(name.encode()).digest() for name in ('mrtd', 'rtmr0', 'rtmr1', 'rtmr2', 'rtmr3')}
    fields = {'td_attributes': bytes(8), 'xfam': bytes(8), **registers}
    body = (
        bytes(16 + 48 + 48 + 8)
        + fields['td_attributes']
        + fields['xfam']
        + fields['mrtd']
        + bytes(48 * 3)
        + b''.join(fields[f'rtmr{i}'] for i in range(4))
        + report_data
    )
    return header + body + bytes(4), image_measurement(fields)


def issue(result: dict, seed_block: int, image: str, config: dict, who: dict) -> Attestation:
    """The attestation Cathedral would return for ``result``, run for ``who`` ({github_login, github_id, hotkey})."""
    their_digest = digest(result, seed_block, image).hex()
    claims = {
        'attestation_digest': their_digest,
        'challenge': {**{k: config[k] for k in ('challenge_id', 'module', 'tier', 'seeds')}, 'repo': 'o/r'},
        'evaluator': {'image': image, 'egress': 'none', 'runner': 'cathedral-sealed-p0'},
        'run': {
            **{'seed_block': seed_block, 'seed_block_hash': result['seed_block_hash'], 'attempt': 1, 'exit_code': 0},
            **{'run_id': '0b8f6a52-6a4e-4d8e-9a55-1f3c2d4e5f60', 'network': 'finney'},
        },
        'submission': {
            **who,
            'hotkey_signature': None,
            'challenger_path': f'solvers/{who["github_login"]}/1',
            'challenger_tree_sha': result['challenger']['sha'],
            'king_path': 'baselines/good',
            'king_tree_sha': result['king']['sha'],
        },
        'outputs': {'result_sha256': hashlib.sha256(canonical(result).encode()).hexdigest(), 'crown': result['crown']},
    }
    commitment = hashlib.sha256(CLAIMS_DOMAIN + receipt_bytes(claims)).digest()
    report_data = hashlib.sha256((commitment.hex() + E2E_KEY).encode()).digest() + bytes(32)
    raw, measurement = quote(report_data)
    receipt = {
        'schema': 'cathedral_challenge_receipt_v1',
        'receipt_id': '7c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f',
        'issued_at': '2026-10-07T14:22:49.000000Z',
        'claims': claims,
        'tee': {
            **{'type': 'intel_tdx', 'binding': 'rd-guestquote-v0', 'e2e_pubkey_b64': E2E_KEY},
            **{'quote_b64': base64.b64encode(raw).decode(), 'quote_sha256': hashlib.sha256(raw).hexdigest()},
            **{'collateral_b64': None, 'image_measurement': measurement},
        },
        'trust': {'claimed_level': 'platform', 'runner_measured': False},
        'signer': {'kind': 'cathedral_platform', 'key_id': KEY_ID, 'algorithm': 'ed25519'},
        'ext': {},
    }
    signature = eddsa.new(KEY, 'rfc8032').sign(receipt_bytes(receipt))
    receipt['signature'] = {'algorithm': 'ed25519', 'value_base64': base64.b64encode(signature).decode()}
    legacy = eddsa.new(KEY, 'rfc8032').sign(bytes.fromhex(their_digest)).hex()
    signer = {'kind': 'cathedral', 'key_id': KEY_ID, 'pubkey': public_hex(KEY), 'receipt': receipt}
    return Attestation.from_json(
        json.dumps({'result': result, 'seed_block': seed_block, 'image': image, 'signer': signer, 'signature': legacy})
    )
