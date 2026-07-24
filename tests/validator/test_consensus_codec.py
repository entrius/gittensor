# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Tests for the weight consensus payload codec."""

import zlib

import pytest

from gittensor.validator.weight_consensus.codec import (
    U16_MAX,
    CodecError,
    canonicalize_prefs,
    decode_prefs,
    encode_prefs,
)


def _encode_plaintext(plaintext: str) -> bytes:
    return zlib.compress(plaintext.encode('ascii'), 9)


class TestEncodeDecode:
    def test_roundtrip(self):
        prefs = canonicalize_prefs({'entrius/gittensor': 0.5, 'autovara/kata': 0.3, 'a/b': 0.2})
        assert decode_prefs(encode_prefs(prefs)) == prefs

    def test_decode_rejects_bad_version(self):
        assert decode_prefs(_encode_plaintext('v2|a/b:100')) is None

    def test_decode_rejects_undecompressable_bytes(self):
        assert decode_prefs(b'\x00garbage') is None

    @pytest.mark.parametrize('entry', ['a/b', 'a/b:', 'a/b:x', 'a/b:1.5', 'a/b:-1', 'a/b:65536', ':100', 'noslash:100'])
    def test_decode_rejects_malformed_entries(self, entry):
        assert decode_prefs(_encode_plaintext(f'v1|{entry}')) is None

    def test_decode_rejects_too_many_repos(self):
        plaintext = 'v1|' + '|'.join(f'a/repo{i}:100' for i in range(11))
        assert decode_prefs(_encode_plaintext(plaintext)) is None

    def test_decode_lowercases_repo_names(self):
        assert decode_prefs(_encode_plaintext('v1|Owner/Repo:100')) == {'owner/repo': 100}

    def test_decode_drops_zero_weights_and_rejects_empty_vector(self):
        assert decode_prefs(_encode_plaintext('v1|a/b:0|c/d:100')) == {'c/d': 100}
        assert decode_prefs(_encode_plaintext('v1|a/b:0')) is None

    def test_decode_rejects_duplicates_after_lowercasing(self):
        assert decode_prefs(_encode_plaintext('v1|a/b:100|A/B:200')) is None
        assert decode_prefs(_encode_plaintext('v1|a/b:0|a/b:200')) is None

    def test_decode_rejects_oversized_plaintext(self):
        assert decode_prefs(zlib.compress(b'v1|' + b'a' * 100_000)) is None

    def test_encode_raises_on_too_many_repos(self):
        with pytest.raises(CodecError):
            encode_prefs({f'a/repo{i}': 100 for i in range(11)})

    def test_encode_raises_on_invalid_weight(self):
        with pytest.raises(CodecError):
            encode_prefs({'a/b': 0})
        with pytest.raises(CodecError):
            encode_prefs({'a/b': U16_MAX + 1})


class TestCanonicalize:
    def test_top10_by_weight_quantized_to_u16_sum(self):
        raw = {f'a/repo{i:02d}': float(i + 1) for i in range(15)}
        prefs = canonicalize_prefs(raw)
        assert len(prefs) == 10
        assert sum(prefs.values()) == U16_MAX
        assert set(prefs) == {f'a/repo{i:02d}' for i in range(5, 15)}

    def test_deterministic_ties_and_duplicate_merge(self):
        assert canonicalize_prefs({'A/B': 1.0, 'a/b': 1.0, 'c/d': 2.0}) == canonicalize_prefs({'a/b': 2.0, 'c/d': 2.0})

    def test_raises_on_empty_or_nonpositive(self):
        with pytest.raises(CodecError):
            canonicalize_prefs({})
        with pytest.raises(CodecError):
            canonicalize_prefs({'a/b': 0.0, 'c/d': -1.0})

    def test_raises_on_invalid_name(self):
        with pytest.raises(CodecError):
            canonicalize_prefs({'not a repo': 1.0})
