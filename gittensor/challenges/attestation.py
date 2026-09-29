# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``attestation.json``: who vouches for a ``gitt challenge eval`` result. The signature is over
``sha256(canonical({result, seed_block, image}))``.

* ``dev``: a local ed25519 key (a 32-byte seed file, created 0600 on first use). Proves only which key signed, so a
  maintainer accepts it only from the challenge's ``dev_attestation_pubkey``; it exists for dry runs.
* ``polaris``: the evaluator's key released by a Polaris appraisal of the blessed image. Not wired yet.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from Crypto.PublicKey.ECC import EccKey
from Crypto.Signature import eddsa

from gittensor.challenges.head_to_head import canonical

DEV, POLARIS = 'dev', 'polaris'
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
    return eddsa.import_private_key(path.read_bytes())


def sign_dev(key_path: str | Path, result: dict, seed_block: int, image: str | None = None) -> Attestation:
    key = dev_key(key_path)
    signature = eddsa.new(key, 'rfc8032').sign(digest(result, seed_block, image))
    pubkey = key.public_key().export_key(format='raw').hex()
    return Attestation(result, seed_block, image, {'kind': DEV, 'pubkey': pubkey}, signature.hex())


def verify(att: Attestation, dev_pubkey: str | None) -> bool:
    """The signature is genuine, and a ``dev`` signer is exactly ``dev_pubkey`` (``None`` accepts no dev signer)."""
    kind = att.signer.get('kind')
    if kind == POLARIS:
        raise NotImplementedError(f'Polaris attestation is not wired yet; see {POLARIS_DOCS}')
    if kind != DEV or not dev_pubkey or att.signer.get('pubkey') != dev_pubkey:
        return False
    try:
        key = eddsa.import_public_key(bytes.fromhex(dev_pubkey))
        eddsa.new(key, 'rfc8032').verify(digest(att.result, att.seed_block, att.image), bytes.fromhex(att.signature))
    except (TypeError, ValueError):
        return False
    return True
