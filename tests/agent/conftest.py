# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Fixtures for the compute agent tests: an ephemeral controller keypair, a fake clock, a signer."""

from __future__ import annotations

import pytest
from bittensor_wallet import Keypair

from gittensor.agent.auth import ACTION_INSTALL, NonceCache, Verifier, sign_request

# A real ed25519 public key (generated for the tests; no private half anywhere).
TEST_PUBKEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGl5D2hMEbW2HqbXQ0Xj2sGvlTBLjYiDwq7nCjQmVnpB controller-op-1'
TEST_PUBKEY_BODY = TEST_PUBKEY.rsplit(' ', 1)[0]
OTHER_PUBKEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBcJqV0O6aY3TkJ8k1jZsvT3E7g1d0d6jWZ0Gz1M3S5Q other'


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(scope='session')
def controller_keypair() -> Keypair:
    return Keypair.create_from_uri('//GittensorTestController')


@pytest.fixture(scope='session')
def attacker_keypair() -> Keypair:
    return Keypair.create_from_uri('//GittensorTestAttacker')


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def verifier(controller_keypair, clock) -> Verifier:
    return Verifier(
        controller_hotkey=controller_keypair.ss58_address, max_skew_s=60, nonces=NonceCache(120), clock=clock
    )


@pytest.fixture
def signed(controller_keypair, clock):
    """signed(action=..., pubkey=..., nonce=..., timestamp=..., keypair=..., hotkey_ss58=...) -> request body."""
    counter = {'n': 0}

    def _make(
        action: str = ACTION_INSTALL,
        pubkey: str = TEST_PUBKEY,
        nonce: str | None = None,
        timestamp: int | None = None,
        keypair: Keypair = controller_keypair,
        hotkey_ss58: str | None = None,
    ) -> dict:
        counter['n'] += 1
        nonce = nonce or f'nonce-{counter["n"]:016d}'
        timestamp = int(clock.now) if timestamp is None else timestamp
        return {
            'pubkey': pubkey,
            'hotkey_ss58': hotkey_ss58 or controller_keypair.ss58_address,
            'nonce': nonce,
            'timestamp': timestamp,
            'signature': sign_request(keypair, action, pubkey, nonce, timestamp),
        }

    return _make
