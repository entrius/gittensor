# The MIT License (MIT)
# Copyright © 2026 Entrius

"""The Cathedral receipt inside ``attestation.json`` (``signer.kind == 'cathedral'``, the receipt at ``signer.receipt``).

Cathedral (cathedral.computer) runs the official run itself in a fresh TDX VM that the miner has no shell in, and signs
a receipt that binds the result, the solver, the image, the seed block, the attempt count and who submitted it. The
format is ``cathedral_challenge_receipt_v1``. ``attestation.verify`` has already checked the signature over gittensor's
own digest with a key pinned in ``challenge.json``; ``receipt_error`` checks the rest, offline, and never runs code.

Not checked here: Intel's signature chain over the TDX quote. Without it the trust level is ``platform`` (Cathedral's
signature, a quote that is well formed and bound to the receipt) and ``min_trust: hardware`` is refused.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from gittensor.challenges.attestation import CATHEDRAL, POLARIS, Attestation, digest, ed25519_ok, trusted_pubkey
from gittensor.challenges.head_to_head import canonical

SCHEMA = 'cathedral_challenge_receipt_v1'
TRUST_LEVELS = ('platform', 'hardware')
PLATFORM_SIGNER = 'cathedral_platform'  # what a receipt calls its own signer, as against ``signer.kind`` outside it
CLAIMS_DOMAIN = b'cathedral-challenge-receipt-v1/claims\0'
IMAGE_DOMAIN = b'cathedral-tdx-image-v1\0'
TOP_KEYS = {'schema', 'receipt_id', 'issued_at', 'claims', 'tee', 'trust', 'signer', 'ext', 'signature'}
CLAIM_KEYS = {'attestation_digest', 'challenge', 'evaluator', 'run', 'submission', 'outputs'}
QUOTE_HEADER = 48
QUOTE_BODY = (  # the TDX v4 report body: (field, bytes), in order
    *(('tee_tcb_svn', 16), ('mrseam', 48), ('mrsignerseam', 48), ('seam_attributes', 8), ('td_attributes', 8)),
    *(('xfam', 8), ('mrtd', 48), ('mrconfigid', 48), ('mrowner', 48), ('mrownerconfig', 48)),
    *(('rtmr0', 48), ('rtmr1', 48), ('rtmr2', 48), ('rtmr3', 48), ('report_data', 64)),
)


@dataclass(frozen=True)
class Author:
    """Who opened the PR: the receipt must name this GitHub account. ``hotkey`` is ``None`` when not looked up."""

    login: str
    github_id: int
    hotkey: str | None = None


def receipt_bytes(doc: object) -> bytes:
    """Cathedral's canonical bytes: sorted keys, compact, ASCII, no NaN. Differs from ``canonical`` only in escaping."""
    return json.dumps(doc, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def has_float(value: object) -> bool:
    if isinstance(value, dict):
        return any(has_float(v) for v in value.values())
    if isinstance(value, list):
        return any(has_float(v) for v in value)
    return isinstance(value, float)


def timestamp(value: str) -> datetime:
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ')


def quote_fields(quote: bytes) -> dict[str, bytes]:
    """The fields of a TDX v4 quote's report body. Raises ``ValueError`` when it is not one."""
    if len(quote) < QUOTE_HEADER + sum(size for _, size in QUOTE_BODY):
        raise ValueError('the quote is shorter than a TDX v4 quote')
    if int.from_bytes(quote[0:2], 'little') != 4 or int.from_bytes(quote[4:8], 'little') != 0x81:
        raise ValueError('the quote is not a TDX v4 quote')
    fields, offset = {}, QUOTE_HEADER
    for name, size in QUOTE_BODY:
        fields[name], offset = quote[offset : offset + size], offset + size
    return fields


def image_measurement(fields: dict[str, bytes]) -> str:
    h = hashlib.sha256(IMAGE_DOMAIN)
    for name in ('td_attributes', 'xfam', 'mrtd', 'rtmr0', 'rtmr1', 'rtmr2', 'rtmr3'):
        h.update(fields[name])
    return f'tdx-image-sha256:{h.hexdigest()}'


def pinned_error(config) -> str | None:
    """Why no Cathedral attestation can be accepted under ``config`` yet, or ``None``: challenge.json does not name
    Cathedral, or pins no key that parses (a placeholder). A miner checks this before paying for a run."""
    policy = config.attestation
    if policy.get('kind') not in (CATHEDRAL, POLARIS):
        return 'challenge.json does not name Cathedral as its attestation'
    keys = policy.get('trusted_keys')
    if not isinstance(keys, dict) or not any(trusted_pubkey(keys, key_id) for key_id in keys):
        return 'challenge.json pins no usable Cathedral key yet'
    return None


def receipt_error(att: Attestation, config, author: Author | None = None) -> str | None:
    """Why the attestation's Cathedral receipt is not acceptable for ``config``, or ``None``. When challenge.json says
    ``attestation.kind`` is ``cathedral`` (or ``polaris``, the platform's planned name, whose signer is Cathedral) any
    other signer is refused, a dev key included, so relabeling a Cathedral
    attestation as a dev one never skips the receipt. Otherwise a signer that is not ``cathedral`` has no receipt:
    ``None``. ``author`` adds the check that the receipt names the PR's author."""
    policy, kind = config.attestation, att.signer.get('kind')
    if policy.get('kind') in (CATHEDRAL, POLARIS) and kind != CATHEDRAL:
        return f'challenge.json accepts only a Cathedral attestation, not a {kind!r} signer'
    if kind != CATHEDRAL:
        return None
    try:
        return _receipt_error(att, config, policy, author)
    except (KeyError, TypeError, ValueError, AttributeError, binascii.Error) as e:
        return f'the receipt is malformed ({e!r})'


def _receipt_error(att: Attestation, config, policy: dict, author: Author | None) -> str | None:
    receipt = att.signer.get('receipt')
    if not isinstance(receipt, dict) or receipt.get('schema') != SCHEMA:
        return f'no {SCHEMA} in signer.receipt'
    if set(receipt) != TOP_KEYS or set(receipt['claims']) != CLAIM_KEYS:
        return 'the receipt has missing or unknown fields'
    if has_float(receipt):
        return 'the receipt holds a float'
    pubkey = trusted_pubkey(policy.get('trusted_keys'), att.signer.get('key_id'))
    entry = policy['trusted_keys'][att.signer['key_id']] if pubkey else {}
    if pubkey is None or not timestamp(entry['valid_from']) <= timestamp(receipt['issued_at']) < timestamp(
        entry['valid_until']
    ):
        return f'key {att.signer.get("key_id")} is not a trusted key for a receipt issued {receipt["issued_at"]}'
    body = {k: v for k, v in receipt.items() if k != 'signature'}
    if not ed25519_ok(pubkey, base64.b64decode(receipt['signature']['value_base64']).hex(), receipt_bytes(body)):
        return 'the receipt signature does not verify'

    claims, result = receipt['claims'], att.result
    attested = digest(att.result, att.seed_block, att.image).hex()
    result_sha = hashlib.sha256(canonical(result).encode()).hexdigest()
    run, sub, out = claims['run'], claims['submission'], claims['outputs']
    # Cathedral counts runs per (GitHub id, challenge, round): a round is one KING, so editing the solver does not
    # reset the count. The cap is the maintainer's: absent, attempts are only recorded.
    attempt, cap = run['attempt'], policy.get('max_attempts')
    if type(attempt) is not int or attempt < 1:
        return f'the receipt attempt {attempt!r} is not a run count from 1'
    if cap is not None and (type(cap) is not int or attempt > cap):
        return f'attempt {attempt} is over max_attempts {cap!r}: challenge.json allows {cap!r} runs per miner and round'
    if (level := receipt['trust']['claimed_level']) not in TRUST_LEVELS:
        return f'the receipt claims trust level {level!r}, not one of {", ".join(TRUST_LEVELS)}'
    expected = {
        'receipt signer key': (receipt['signer']['key_id'], att.signer['key_id']),
        'receipt signer kind': (receipt['signer']['kind'], PLATFORM_SIGNER),
        'receipt signer algorithm': (receipt['signer']['algorithm'], 'ed25519'),
        'attestation digest': (claims['attestation_digest'], attested),
        'result hash': (out['result_sha256'], result_sha),
        'challenge id': (claims['challenge']['challenge_id'], config.challenge_id),
        'module': (claims['challenge']['module'], config.module),
        'tier': (claims['challenge']['tier'], config.tier),
        'seeds': (claims['challenge']['seeds'], config.seeds),
        'image': (claims['evaluator']['image'], config.image),
        'egress': (claims['evaluator']['egress'], 'none'),
        'seed block': (run['seed_block'], att.seed_block),
        'seed block hash': (run['seed_block_hash'], result.get('seed_block_hash')),
        'exit code': (run['exit_code'], 0),
        'challenger sha': (sub['challenger_tree_sha'], result['challenger']['sha']),
        'king sha': (sub['king_tree_sha'], result['king']['sha']),
        'crown': (out['crown'], result['crown']),
    }
    if 'repo' in policy:  # which challenge repo's runs these are; absent, the challenge id above is all there is
        expected['repo'] = (claims['challenge']['repo'], policy['repo'])
    if wrong := [f'{name} ({got!r} is not {want!r})' for name, (got, want) in expected.items() if got != want]:
        return f'the receipt does not match the attestation or challenge.json: {", ".join(wrong)}'
    if error := quote_error(receipt, claims, policy):
        return error
    if policy.get('min_trust', 'platform') != 'platform':
        return f'min_trust {policy["min_trust"]!r} needs the Intel quote chain check, which is not built in yet'
    if author:
        return author_error(sub, author)
    return None


def quote_error(receipt: dict, claims: dict, policy: dict) -> str | None:
    """The TDX quote is well formed, not a debug TD, and carries the receipt's claims in its report data."""
    tee = receipt['tee']
    quote = base64.b64decode(tee['quote_b64'], validate=True)
    if tee['type'] != 'intel_tdx' or hashlib.sha256(quote).hexdigest() != tee['quote_sha256']:
        return 'the receipt has no TDX quote, or its hash is wrong'
    fields = quote_fields(quote)
    if fields['td_attributes'][0] & 1:
        return 'the quote is from a debug TD'
    commitment = hashlib.sha256(CLAIMS_DOMAIN + receipt_bytes(claims)).digest()
    binding = tee['binding']
    if binding == 'rd-claims-v1':
        bound = fields['report_data'] == commitment + bytes(32)
    elif binding == 'rd-guestquote-v0':
        bound = (
            fields['report_data'][:32] == hashlib.sha256((commitment.hex() + tee['e2e_pubkey_b64']).encode()).digest()
        )
    else:
        return f'unknown quote binding {binding!r}'
    if not bound:
        return 'the quote does not carry these claims'
    if image_measurement(fields) != tee['image_measurement']:
        return 'the quote does not match the receipt image measurement'
    allowed = policy.get('tdx_image_measurements') or []
    if binding == 'rd-claims-v1' and allowed and tee['image_measurement'] not in allowed:
        return f'runner measurement {tee["image_measurement"]} is not in challenge.json'
    return None


def author_error(sub: dict, author: Author) -> str | None:
    """The receipt was issued to this GitHub account (and hotkey), for a solver under its own name."""
    if sub['github_id'] != author.github_id or sub['github_login'].lower() != author.login.lower():
        return f'the receipt was issued to {sub["github_login"]} ({sub["github_id"]}), not {author.login} ({author.github_id})'
    if sub['challenger_path'].split('/')[1:2] != [author.login]:
        return f'the receipt ran {sub["challenger_path"]}, not a solver of {author.login}'
    if author.hotkey and sub['hotkey'] != author.hotkey:
        return f'the receipt was issued to hotkey {sub["hotkey"]}, not {author.hotkey}'
    return None
