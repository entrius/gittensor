# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for synapse PAT redaction in repr/str (issue #850)."""

import pytest

from gittensor.synapses import PatBroadcastSynapse


SAMPLE_PAT = 'ghp_SUPERSECRET12345'


# ==========================================================================
# TestPatRedaction
# ==========================================================================


class TestPatRedaction:
    """The raw PAT must never appear in repr/str output."""

    def test_repr_does_not_contain_raw_token(self):
        syn = PatBroadcastSynapse(github_access_token=SAMPLE_PAT)
        assert SAMPLE_PAT not in repr(syn)

    def test_str_does_not_contain_raw_token(self):
        syn = PatBroadcastSynapse(github_access_token=SAMPLE_PAT)
        assert SAMPLE_PAT not in str(syn)

    def test_repr_keeps_last_four_chars(self):
        """Operators correlate masked log lines with rotated tokens via the last 4 chars."""
        syn = PatBroadcastSynapse(github_access_token=SAMPLE_PAT)
        assert '***2345' in repr(syn)

    def test_repr_includes_other_fields(self):
        syn = PatBroadcastSynapse(
            github_access_token=SAMPLE_PAT,
            accepted=True,
            rejection_reason='something',
        )
        r = repr(syn)
        assert 'accepted=True' in r
        assert "rejection_reason='something'" in r


# ==========================================================================
# TestEdgeCases
# ==========================================================================


class TestEdgeCases:
    """Short/empty tokens must not crash repr."""

    def test_short_token_fully_masked(self):
        """Tokens shorter than 4 chars are fully masked — no IndexError, no leak."""
        syn = PatBroadcastSynapse(github_access_token='abc')
        r = repr(syn)
        assert 'abc' not in r
        assert '***' in r

    def test_empty_token_does_not_crash(self):
        syn = PatBroadcastSynapse(github_access_token='')
        r = repr(syn)
        assert '***' in r

    def test_exactly_four_char_token_masked(self):
        """Boundary case: exactly 4 chars — currently shown as last 4."""
        syn = PatBroadcastSynapse(github_access_token='abcd')
        r = repr(syn)
        assert '***abcd' in r


# ==========================================================================
# TestWireFormatUnchanged
# ==========================================================================


class TestWireFormatUnchanged:
    """The wire format must still carry the full token — only repr/str redact."""

    def test_model_dump_json_includes_full_token(self):
        syn = PatBroadcastSynapse(github_access_token=SAMPLE_PAT)
        payload = syn.model_dump_json()
        assert SAMPLE_PAT in payload

    def test_attribute_access_returns_full_token(self):
        """Code that needs the token (validator handlers) still gets it via attribute access."""
        syn = PatBroadcastSynapse(github_access_token=SAMPLE_PAT)
        assert syn.github_access_token == SAMPLE_PAT
