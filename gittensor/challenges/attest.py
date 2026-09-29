# The MIT License (MIT)
# Copyright © 2026 Entrius

"""Who vouches for a scorecard. The attestation signs the sha256 of the document's canonical bytes without its
``attestation`` field (it cannot sign itself).

* ``dev``: a local ed25519 key (a 32-byte seed file, created 0600 on first use). Proves only which key wrote it.
* ``polaris``: the evaluator's key released by a Polaris appraisal of the blessed image. Not wired yet.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from Crypto.Signature import eddsa

from gittensor.controller.pay.scorecard import canonical_bytes

DEV = 'dev'
POLARIS_DOCS = 'https://docs.fr0ntierx.com/attestation/'


def signed_sha256(doc: dict) -> str:
    return hashlib.sha256(canonical_bytes({k: v for k, v in doc.items() if k != 'attestation'})).hexdigest()


class DevAttestor:
    def __init__(self, key_path: str | Path):
        path = Path(key_path).expanduser()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(os.urandom(32))
        self._key = eddsa.import_private_key(path.read_bytes())
        self.public_key = self._key.public_key().export_key(format='raw').hex()

    def attest(self, doc: dict) -> dict:
        sha = signed_sha256(doc)
        signature = eddsa.new(self._key, 'rfc8032').sign(sha.encode())
        return {'kind': DEV, 'public_key': self.public_key, 'sha256': sha, 'signature': signature.hex()}


class PolarisAttestor:
    def attest(self, doc: dict) -> dict:
        raise NotImplementedError(f'Polaris attestation is not wired yet; see {POLARIS_DOCS}')


def verify_dev(doc: dict) -> bool:
    """The ``dev`` attestation is a valid signature over this document. Says nothing about whose key it is."""
    att = doc.get('attestation') or {}
    if att.get('kind') != DEV or att.get('sha256') != signed_sha256(doc):
        return False
    try:
        key = eddsa.import_public_key(bytes.fromhex(att['public_key']))
        eddsa.new(key, 'rfc8032').verify(att['sha256'].encode(), bytes.fromhex(att['signature']))
    except (KeyError, TypeError, ValueError):
        return False
    return True
