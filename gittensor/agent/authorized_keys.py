# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Root's ``authorized_keys`` as the agent edits it: append and remove, idempotently, atomically.

Keys the controller installs carry a comment tag (:data:`~gittensor.agent.config.KEY_COMMENT_TAG`) so an
operator reading the file can tell them from anything else, and so the agent can list what it added. Lines the
agent did not write are preserved byte-for-byte.
"""

from __future__ import annotations

import base64
import os
import struct
import tempfile
from pathlib import Path

from gittensor.agent.config import KEY_COMMENT_TAG

ALLOWED_KEY_TYPES = frozenset(
    {
        'ssh-ed25519',
        'ssh-rsa',
        'ecdsa-sha2-nistp256',
        'ecdsa-sha2-nistp384',
        'ecdsa-sha2-nistp521',
        'sk-ssh-ed25519@openssh.com',
        'sk-ecdsa-sha2-nistp256@openssh.com',
    }
)
MAX_PUBKEY_CHARS = 4096


class InvalidPublicKey(ValueError):
    """Not a public key sshd would accept."""


def normalize_pubkey(text: str) -> str:
    """Return ``'<type> <base64>'`` for a public key line, dropping any comment.

    Checks the type is one sshd knows, the blob is valid base64, and the blob's own leading type string matches
    the declared type (so a key cannot claim one algorithm and carry another).
    """
    if not isinstance(text, str) or len(text) > MAX_PUBKEY_CHARS:
        raise InvalidPublicKey('key text missing or too long')
    parts = text.strip().split()
    if len(parts) < 2:
        raise InvalidPublicKey('expected "<type> <base64> [comment]"')
    key_type, blob_b64 = parts[0], parts[1]
    if key_type not in ALLOWED_KEY_TYPES:
        raise InvalidPublicKey(f'unsupported key type {key_type!r}')
    try:
        blob = base64.b64decode(blob_b64, validate=True)
    except (ValueError, TypeError):
        raise InvalidPublicKey('key blob is not valid base64') from None
    if len(blob) < 4:
        raise InvalidPublicKey('key blob too short')
    (type_len,) = struct.unpack('>I', blob[:4])
    if blob[4 : 4 + type_len] != key_type.encode():
        raise InvalidPublicKey('key blob does not match its declared type')
    return f'{key_type} {blob_b64}'


def _key_body(line: str) -> str | None:
    """``'<type> <base64>'`` from an authorized_keys line (options prefix tolerated), or None for non-key lines."""
    tokens = line.strip().split()
    for i, token in enumerate(tokens[:-1]):
        if token in ALLOWED_KEY_TYPES:
            return f'{token} {tokens[i + 1]}'
    return None


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text().splitlines()
    except FileNotFoundError:
        return []


def _write_atomic(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.authorized_keys.')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(''.join(f'{line}\n' for line in lines))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def install_key(path: str | os.PathLike, pubkey: str, tag: str = KEY_COMMENT_TAG) -> bool:
    """Append ``pubkey`` (normalized, tagged) unless a line with the same key body exists. True if written."""
    path = Path(path)
    body = normalize_pubkey(pubkey)
    lines = _read_lines(path)
    if any(_key_body(line) == body for line in lines):
        return False
    _write_atomic(path, [*lines, f'{body} {tag}'])
    return True


def remove_key(path: str | os.PathLike, pubkey: str) -> bool:
    """Drop every line whose key body matches ``pubkey``. True if anything was removed."""
    path = Path(path)
    body = normalize_pubkey(pubkey)
    lines = _read_lines(path)
    kept = [line for line in lines if _key_body(line) != body]
    if len(kept) == len(lines):
        return False
    _write_atomic(path, kept)
    return True


def tagged_keys(path: str | os.PathLike, tag: str = KEY_COMMENT_TAG) -> list[str]:
    """Key bodies of the lines the agent installed (those ending in ``tag``)."""
    out = []
    for line in _read_lines(Path(path)):
        body = _key_body(line)
        if body and line.strip().endswith(f' {tag}'):
            out.append(body)
    return out
