# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``attestation.json``: who vouches for a ``gitt challenge eval`` result. The signature is over
``sha256(canonical({result, seed_block, image}))``.

* ``dev``: a local ed25519 key (a 32-byte seed file, created 0600 on first use). Proves only which key signed, so a
  maintainer accepts it only from the challenge's ``dev_attestation_pubkey``; it exists for dry runs.
* ``polaris``: the evaluator's key released by a Polaris appraisal of the blessed image. Not wired yet.
* ``cathedral``: Cathedral (cathedral.computer) ran the official run itself and signed with a platform key the
  challenge pins in ``challenge.json`` (``attestation.trusted_keys``). The signature here is the same ed25519 over the
  same digest as ``dev``; the Cathedral receipt in ``signer.receipt`` is checked by ``challenges/cathedral.py``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from Crypto.PublicKey.ECC import EccKey
from Crypto.Signature import eddsa

from gittensor.challenges.head_to_head import canonical

DEV, POLARIS, CATHEDRAL = 'dev', 'polaris', 'cathedral'
TRUSTED_STATUSES = ('active', 'retired')  # a revoked key fails every receipt, whatever its date
POLARIS_DOCS = 'https://docs.fr0ntierx.com/attestation/'


@dataclass(frozen=True)
class Attestation:
    result: dict
    seed_block: int
    image: str | None
    signer: dict
    signature: str

    def to_json(self) -> str:
        return canonical(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> Attestation:
        """Raises ``ValueError`` on anything that is not an attestation's shape."""
        try:
            doc = json.loads(text)
            att = cls(**{name: doc[name] for name in cls.__dataclass_fields__})
        except (TypeError, KeyError, json.JSONDecodeError) as e:
            raise ValueError(f'not an attestation: {e}') from e
        if not (isinstance(att.result, dict) and isinstance(att.signer, dict) and type(att.seed_block) is int):
            raise ValueError('not an attestation: result, signer or seed_block has the wrong type')
        return att


def canonical_payload(result: dict, seed_block: int, image: str | None) -> bytes:
    return canonical({'result': result, 'seed_block': seed_block, 'image': image}).encode()


def digest(result: dict, seed_block: int, image: str | None) -> bytes:
    return hashlib.sha256(canonical_payload(result, seed_block, image)).digest()


def dev_key(key_path: str | Path) -> EccKey:
    path = Path(key_path).expanduser()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(os.urandom(32))
    seed = path.read_bytes()
    if len(seed) != 32:
        raise ValueError(f'{path} is not a 32-byte ed25519 seed')
    return eddsa.import_private_key(seed)


def public_hex(key: EccKey) -> str:
    return key.public_key().export_key(format='raw').hex()


def dev_pubkey(key_path: str | Path) -> str:
    """The dev key's public key (hex), making the key file on first use."""
    return public_hex(dev_key(key_path))


def sign_dev(key_path: str | Path, result: dict, seed_block: int, image: str | None = None) -> Attestation:
    key = dev_key(key_path)
    signature = eddsa.new(key, 'rfc8032').sign(digest(result, seed_block, image))
    return Attestation(result, seed_block, image, {'kind': DEV, 'pubkey': public_hex(key)}, signature.hex())


def trusted_pubkey(trusted_keys: dict | None, key_id: object) -> bytes | None:
    """The raw public key ``challenge.json`` pins under ``key_id`` (``trusted_keys``), or ``None`` when it is unknown,
    not ed25519, revoked or malformed. The key is never taken from the attestation itself."""
    entry = (trusted_keys or {}).get(key_id) if isinstance(key_id, str) else None
    if (
        not isinstance(entry, dict)
        or entry.get('algorithm') != 'ed25519'
        or entry.get('status') not in TRUSTED_STATUSES
    ):
        return None
    try:
        key = base64.b64decode(entry['public_key_base64'], validate=True)
    except (KeyError, TypeError, binascii.Error):
        return None
    return key if len(key) == 32 else None


def ed25519_ok(pubkey: bytes, signature_hex: str, message: bytes) -> bool:
    try:
        eddsa.new(eddsa.import_public_key(pubkey), 'rfc8032').verify(message, bytes.fromhex(signature_hex))
    except (TypeError, ValueError):
        return False
    return True


def verify(att: Attestation, dev_pubkey: str | None, trusted_keys: dict | None = None) -> bool:
    """The signature is genuine, and a ``dev`` signer is exactly ``dev_pubkey`` (``None`` accepts no dev signer) and a
    ``cathedral`` signer is a key pinned in ``trusted_keys`` (``None`` accepts none)."""
    kind = att.signer.get('kind')
    if kind == POLARIS:
        raise NotImplementedError(f'Polaris attestation is not wired yet; see {POLARIS_DOCS}')
    message = digest(att.result, att.seed_block, att.image)
    if kind == CATHEDRAL:
        pubkey = trusted_pubkey(trusted_keys, att.signer.get('key_id'))
        return (
            pubkey is not None
            and att.signer.get('pubkey') == pubkey.hex()
            and ed25519_ok(pubkey, att.signature, message)
        )
    if kind != DEV or not dev_pubkey or att.signer.get('pubkey') != dev_pubkey:
        return False
    try:
        return ed25519_ok(bytes.fromhex(dev_pubkey), att.signature, message)
    except ValueError:
        return False
