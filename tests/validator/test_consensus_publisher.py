# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Tests for local preference resolution and chain publishing."""

import json
import zlib
from types import SimpleNamespace
from unittest.mock import MagicMock

from gittensor.validator.utils.load_weights import RepositoryConfig
from gittensor.validator.weight_consensus.chain import extract_payload_candidates, extract_prefs
from gittensor.validator.weight_consensus.codec import encode_prefs
from gittensor.validator.weight_consensus.publisher import maybe_publish_prefs, resolve_local_prefs

MASTER = {
    'a/big': RepositoryConfig(emission_share=0.6),
    'b/small': RepositoryConfig(emission_share=0.4),
    'c/zero': RepositoryConfig(emission_share=0.0),
}


def _commitment_value(payload: bytes) -> dict:
    return {'info': {'fields': [[{'BigRaw': '0x' + payload.hex()}]]}}


def _subtensor_with_own_commitment(payload):
    subtensor = MagicMock()
    subtensor.substrate.query.return_value = _commitment_value(payload) if payload else None
    subtensor.sign_and_send_extrinsic.return_value = SimpleNamespace(success=True)
    return subtensor


def _wallet():
    return SimpleNamespace(hotkey=SimpleNamespace(ss58_address='hk-self'))


class TestResolveLocalPrefs:
    def test_default_vote_from_baked_master_shares(self, tmp_path):
        prefs = resolve_local_prefs(tmp_path / 'missing.json', MASTER)
        assert set(prefs) == {'a/big', 'b/small'}
        assert prefs['a/big'] > prefs['b/small']

    def test_prefs_file_parsed(self, tmp_path):
        path = tmp_path / 'prefs.json'
        path.write_text(json.dumps({'version': 1, 'repos': {'X/Y': 3, 'z/w': 1}}))
        prefs = resolve_local_prefs(path, MASTER)
        assert set(prefs) == {'x/y', 'z/w'}

    def test_invalid_file_falls_back_to_default(self, tmp_path):
        path = tmp_path / 'prefs.json'
        path.write_text('{not json')
        assert set(resolve_local_prefs(path, MASTER)) == {'a/big', 'b/small'}


class TestExtractPayload:
    def test_extracts_bigraw_hex_bytes_and_int_lists(self):
        payload = encode_prefs({'a/b': 100})
        for encoded in ('0x' + payload.hex(), payload, list(payload)):
            assert extract_payload_candidates({'info': {'fields': [[{'BigRaw': encoded}]]}}) == [payload]

    def test_prefers_first_valid_prefs_field(self):
        payload = encode_prefs({'a/b': 100})
        value = {'info': {'fields': [[{'Raw16': '0x' + b'not a vector 123'.hex()}, {'BigRaw': '0x' + payload.hex()}]]}}
        assert extract_prefs(value) == payload

    def test_no_fields_returns_none(self):
        assert extract_prefs({'info': {'fields': []}}) is None
        assert extract_prefs(None) is None


class TestMaybePublish:
    def test_skips_when_onchain_decodes_equal(self):
        prefs = {'a/b': 60000, 'c/d': 5535}
        subtensor = _subtensor_with_own_commitment(encode_prefs(prefs))
        assert maybe_publish_prefs(subtensor, _wallet(), 74, prefs) is True
        subtensor.sign_and_send_extrinsic.assert_not_called()

    def test_skip_compares_decoded_prefs_not_compressed_bytes(self):
        prefs = {'a/b': 65535}
        plaintext = 'v1|a/b:65535'
        subtensor = _subtensor_with_own_commitment(zlib.compress(plaintext.encode(), 1))
        assert maybe_publish_prefs(subtensor, _wallet(), 74, prefs) is True
        subtensor.sign_and_send_extrinsic.assert_not_called()

    def test_publishes_bigraw_double_nested_info_signed_by_hotkey(self):
        prefs = {'a/b': 65535}
        subtensor = _subtensor_with_own_commitment(None)
        assert maybe_publish_prefs(subtensor, _wallet(), 74, prefs) is True
        _, kwargs = subtensor.sign_and_send_extrinsic.call_args
        assert kwargs['sign_with'] == 'hotkey'

    def test_publish_failure_warns_and_returns_false(self):
        subtensor = _subtensor_with_own_commitment(None)
        subtensor.sign_and_send_extrinsic.side_effect = RuntimeError('rate limited')
        assert maybe_publish_prefs(subtensor, _wallet(), 74, {'a/b': 65535}) is False
