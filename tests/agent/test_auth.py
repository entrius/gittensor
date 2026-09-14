# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Signature verification, replay rejection and payload shape checks for the key-install route."""

import pytest

from gittensor.agent.auth import (
    ACTION_INSTALL,
    ACTION_REMOVE,
    AuthError,
    NonceCache,
    SignedKeyRequest,
    signing_message,
)
from tests.agent.conftest import OTHER_PUBKEY, TEST_PUBKEY


def _verify(verifier, body, action=ACTION_INSTALL):
    verifier.verify(SignedKeyRequest.from_payload(action, body))


class TestHappyPath:
    def test_controller_signature_accepted(self, verifier, signed):
        _verify(verifier, signed())

    def test_remove_signature_accepted_for_remove(self, verifier, signed):
        _verify(verifier, signed(action=ACTION_REMOVE), action=ACTION_REMOVE)

    def test_signature_without_0x_prefix_accepted(self, verifier, signed):
        body = signed()
        body['signature'] = body['signature'][2:]
        _verify(verifier, body)

    def test_timestamp_at_edge_of_window_accepted(self, verifier, signed, clock):
        _verify(verifier, signed(timestamp=int(clock.now) - 60))
        _verify(verifier, signed(timestamp=int(clock.now) + 60))


class TestSadPath:
    def test_attacker_signature_rejected(self, verifier, signed, attacker_keypair):
        with pytest.raises(AuthError, match='does not verify'):
            _verify(verifier, signed(keypair=attacker_keypair))

    def test_attacker_hotkey_in_payload_rejected_before_crypto(self, verifier, signed, attacker_keypair):
        body = signed(keypair=attacker_keypair, hotkey_ss58=attacker_keypair.ss58_address)
        with pytest.raises(AuthError, match='not the controller'):
            _verify(verifier, body)

    def test_substituted_pubkey_rejected(self, verifier, signed):
        body = signed()
        body['pubkey'] = OTHER_PUBKEY  # valid signature, over a different key (Lium issue #744)
        with pytest.raises(AuthError, match='does not verify'):
            _verify(verifier, body)

    def test_install_signature_cannot_remove(self, verifier, signed):
        body = signed(action=ACTION_INSTALL)
        with pytest.raises(AuthError, match='does not verify'):
            _verify(verifier, body, action=ACTION_REMOVE)

    def test_corrupted_signature_rejected(self, verifier, signed):
        body = signed()
        body['signature'] = '0x' + 'ab' * 64
        with pytest.raises(AuthError, match='does not verify'):
            _verify(verifier, body)

    def test_tampered_nonce_rejected(self, verifier, signed):
        body = signed(nonce='nonce-original-000001')
        body['nonce'] = 'nonce-tampered-000001'
        with pytest.raises(AuthError, match='does not verify'):
            _verify(verifier, body)

    def test_tampered_timestamp_rejected(self, verifier, signed, clock):
        body = signed()
        body['timestamp'] = int(clock.now) + 1
        with pytest.raises(AuthError, match='does not verify'):
            _verify(verifier, body)


class TestReplay:
    def test_nonce_reuse_rejected(self, verifier, signed):
        body = signed()
        _verify(verifier, body)
        with pytest.raises(AuthError, match='nonce already used'):
            _verify(verifier, body)

    def test_nonce_not_consumed_by_a_bad_signature(self, verifier, signed, attacker_keypair):
        nonce = 'nonce-shared-0000000001'
        with pytest.raises(AuthError):
            _verify(verifier, signed(keypair=attacker_keypair, nonce=nonce))
        _verify(verifier, signed(nonce=nonce))  # the controller's own use of that nonce still works

    def test_stale_timestamp_rejected(self, verifier, signed, clock):
        body = signed()
        clock.advance(61)
        with pytest.raises(AuthError, match='outside the 60s window'):
            _verify(verifier, body)

    def test_future_timestamp_rejected(self, verifier, signed, clock):
        with pytest.raises(AuthError, match='outside the 60s window'):
            _verify(verifier, signed(timestamp=int(clock.now) + 61))

    def test_nonce_cache_forgets_after_ttl(self):
        cache = NonceCache(ttl_s=120)
        assert cache.check_and_add('a', now=1000)
        assert not cache.check_and_add('a', now=1050)  # inside the ttl: still remembered
        assert cache.check_and_add('b', now=1119)  # 'a' (t=1000) is not yet older than 120 s
        assert len(cache) == 2
        assert cache.check_and_add(
            'a', now=1121
        )  # now it is, and is forgotten (it could not pass the skew check anyway)
        assert len(cache) == 2  # 'b' and the new 'a'


class TestPayloadShape:
    @pytest.mark.parametrize('missing', ['pubkey', 'hotkey_ss58', 'nonce', 'timestamp', 'signature'])
    def test_missing_field_is_400(self, signed, missing):
        body = signed()
        del body[missing]
        with pytest.raises(AuthError, match=missing) as exc:
            SignedKeyRequest.from_payload(ACTION_INSTALL, body)
        assert exc.value.status == 400

    def test_non_object_body_is_400(self):
        with pytest.raises(AuthError) as exc:
            SignedKeyRequest.from_payload(ACTION_INSTALL, ['not', 'an', 'object'])
        assert exc.value.status == 400

    def test_string_timestamp_is_400(self, signed, clock):
        body = signed()
        body['timestamp'] = str(int(clock.now))
        with pytest.raises(AuthError, match='timestamp'):
            SignedKeyRequest.from_payload(ACTION_INSTALL, body)

    def test_short_nonce_is_400(self, signed):
        with pytest.raises(AuthError, match='nonce'):
            SignedKeyRequest.from_payload(ACTION_INSTALL, signed(nonce='short'))

    def test_garbage_pubkey_is_400(self, signed):
        with pytest.raises(AuthError, match='invalid pubkey'):
            SignedKeyRequest.from_payload(ACTION_INSTALL, signed(pubkey='not a key at all'))

    def test_non_hex_signature_is_400(self, signed):
        body = signed()
        body['signature'] = 'zz' * 32
        with pytest.raises(AuthError, match='hex'):
            SignedKeyRequest.from_payload(ACTION_INSTALL, body)

    def test_unknown_action_is_a_programming_error(self):
        with pytest.raises(ValueError):
            signing_message('reboot', TEST_PUBKEY, 'nonce-000000000000001', 0)


def test_signing_message_is_newline_separated_and_domain_prefixed():
    msg = signing_message(ACTION_INSTALL, TEST_PUBKEY, 'nonce-000000000000001', 1700000000)
    assert msg.split(b'\n') == [
        b'gittensor-agent/v1',
        b'install_ssh_key',
        TEST_PUBKEY.encode(),
        b'nonce-000000000000001',
        b'1700000000',
    ]
