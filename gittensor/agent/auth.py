# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Signed-request verification for the agent's key-install route.

The controller signs ``signing_message(action, pubkey, nonce, timestamp)`` with its hotkey (sr25519). The agent
accepts the request only when the signature verifies against the hotkey compiled in at build
(:data:`gittensor.agent.config.CONTROLLER_HOTKEY_SS58`), the timestamp is within
:data:`~gittensor.agent.config.SIGNATURE_MAX_SKEW_S` of the agent's clock, and the nonce has not been seen. The
action is part of the message so an ``install`` signature can never be replayed as a ``remove`` (or vice versa),
and the pubkey is part of it so a captured signature cannot be reused with a substituted key (Lium's issue #744).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from gittensor.agent.authorized_keys import InvalidPublicKey, normalize_pubkey
from gittensor.agent.config import (
    ACTION_INSTALL,
    ACTION_REMOVE,
    CONTROLLER_HOTKEY_SS58,
    NONCE_MAX_LEN,
    NONCE_MIN_LEN,
    SIGNATURE_MAX_SKEW_S,
    SIGNING_DOMAIN,
)

ACTIONS = frozenset({ACTION_INSTALL, ACTION_REMOVE})
_NONCE_RE = re.compile(r'^[A-Za-z0-9_\-]+$')
_HEX_RE = re.compile(r'^(0x)?[0-9a-fA-F]+$')


class AuthError(Exception):
    """A request the agent refuses; ``status`` is the HTTP code the route answers with."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def signing_message(action: str, pubkey: str, nonce: str, timestamp: int) -> bytes:
    """The exact bytes the controller signs. Newline-joined so no field can run into the next."""
    if action not in ACTIONS:
        raise ValueError(f'unknown action {action!r}')
    return '\n'.join([SIGNING_DOMAIN, action, pubkey, nonce, str(int(timestamp))]).encode()


def sign_request(keypair: Any, action: str, pubkey: str, nonce: str, timestamp: int) -> str:
    """Controller-side helper: hex signature (``0x``-prefixed) over :func:`signing_message`.

    ``keypair`` is a ``bittensor_wallet.Keypair`` holding the controller hotkey's private key. Used by the tests
    and by the controller (WS-B) when it mints a per-operation SSH key.
    """
    return '0x' + keypair.sign(signing_message(action, pubkey, nonce, timestamp)).hex()


@dataclass(frozen=True)
class SignedKeyRequest:
    """A parsed ``{pubkey, hotkey_ss58, nonce, timestamp, signature}`` body for one action."""

    action: str
    pubkey: str
    hotkey_ss58: str
    nonce: str
    timestamp: int
    signature: str

    @classmethod
    def from_payload(cls, action: str, payload: Mapping[str, Any]) -> SignedKeyRequest:
        """Shape-check a decoded JSON body; every problem here is a 400, not a 401."""
        if action not in ACTIONS:
            raise ValueError(f'unknown action {action!r}')
        if not isinstance(payload, Mapping):
            raise AuthError('body must be a JSON object', 400)
        missing = [k for k in ('pubkey', 'hotkey_ss58', 'nonce', 'timestamp', 'signature') if k not in payload]
        if missing:
            raise AuthError(f'missing field(s): {", ".join(missing)}', 400)
        pubkey, hotkey, nonce, signature = (payload[k] for k in ('pubkey', 'hotkey_ss58', 'nonce', 'signature'))
        if not all(isinstance(v, str) for v in (pubkey, hotkey, nonce, signature)):
            raise AuthError('pubkey, hotkey_ss58, nonce and signature must be strings', 400)
        timestamp = payload['timestamp']
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise AuthError('timestamp must be an integer (unix seconds)', 400)
        if not (NONCE_MIN_LEN <= len(nonce) <= NONCE_MAX_LEN) or not _NONCE_RE.match(nonce):
            raise AuthError(f'nonce must be {NONCE_MIN_LEN}-{NONCE_MAX_LEN} chars of [A-Za-z0-9_-]', 400)
        if not _HEX_RE.match(signature):
            raise AuthError('signature must be hex', 400)
        try:
            normalize_pubkey(pubkey)
        except InvalidPublicKey as e:
            raise AuthError(f'invalid pubkey: {e}', 400) from None
        return cls(action, pubkey, hotkey, nonce, timestamp, signature)

    def message(self) -> bytes:
        return signing_message(self.action, self.pubkey, self.nonce, self.timestamp)


class NonceCache:
    """Nonces seen, with their timestamps; anything older than ``ttl_s`` is forgotten.

    A nonce only has to be remembered as long as its timestamp could still pass the skew check, so the TTL is
    twice the skew window. Memory is bounded by the request rate over that window, which is one controller.
    """

    def __init__(self, ttl_s: float = 2 * SIGNATURE_MAX_SKEW_S):
        self.ttl_s = ttl_s
        self._seen: dict[str, float] = {}

    def __len__(self) -> int:
        return len(self._seen)

    def prune(self, now: float) -> None:
        cutoff = now - self.ttl_s
        for nonce in [n for n, t in self._seen.items() if t < cutoff]:
            del self._seen[nonce]

    def check_and_add(self, nonce: str, now: float) -> bool:
        """True and remembered if unseen; False if this nonce was already used."""
        self.prune(now)
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True


class Verifier:
    """Checks a :class:`SignedKeyRequest` against the compiled-in controller hotkey."""

    def __init__(
        self,
        controller_hotkey: str = CONTROLLER_HOTKEY_SS58,
        max_skew_s: float = SIGNATURE_MAX_SKEW_S,
        nonces: NonceCache | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.controller_hotkey = controller_hotkey
        self.max_skew_s = max_skew_s
        self.nonces = nonces if nonces is not None else NonceCache(2 * max_skew_s)
        self.clock = clock

    def verify(self, request: SignedKeyRequest) -> None:
        """Raise :class:`AuthError` unless the request is fresh, unseen and signed by the controller.

        Order: hotkey, skew, signature, nonce. The nonce is consumed last so a forged request cannot burn a
        nonce the controller is about to use.
        """
        if request.hotkey_ss58 != self.controller_hotkey:
            raise AuthError('hotkey_ss58 is not the controller this agent trusts')
        now = self.clock()
        if abs(now - request.timestamp) > self.max_skew_s:
            raise AuthError(f'timestamp outside the {self.max_skew_s:g}s window')
        if not _signature_valid(self.controller_hotkey, request.message(), request.signature):
            raise AuthError('signature does not verify against the controller hotkey')
        if not self.nonces.check_and_add(request.nonce, now):
            raise AuthError('nonce already used')


def _signature_valid(ss58: str, message: bytes, signature_hex: str) -> bool:
    # Imported here so the CLI (which imports gittensor.agent.config) never pays for the Rust extension.
    from bittensor_wallet import Keypair

    if not signature_hex.startswith('0x'):
        signature_hex = '0x' + signature_hex  # bittensor_wallet accepts hex strings with the prefix only
    try:
        return bool(Keypair(ss58_address=ss58).verify(message, signature_hex))
    except Exception:  # malformed hex, wrong length, bad ss58: all "does not verify"
        return False
