# The MIT License (MIT)
# Copyright © 2025 Entrius
# GitTensor Utils Tests

"""Unit tests for gittensor.utils.utils module."""

import pytest

from gittensor.utils.utils import mask_secret, parse_repo_name


class TestMaskSecret:
    """Tests for the mask_secret utility function."""

    def test_returns_masked_format(self):
        result = mask_secret("my_secret_token")
        assert result.startswith("<masked:")
        assert result.endswith(">")

    def test_default_length(self):
        result = mask_secret("test")
        # Format: <masked:XXXXX> where XXXXX is 5 hex chars
        hash_part = result[len("<masked:"):-1]
        assert len(hash_part) == 5

    def test_custom_length(self):
        result = mask_secret("test", length=10)
        hash_part = result[len("<masked:"):-1]
        assert len(hash_part) == 10

    def test_deterministic(self):
        """Same input should always produce the same masked output."""
        result1 = mask_secret("same_secret")
        result2 = mask_secret("same_secret")
        assert result1 == result2

    def test_different_inputs_differ(self):
        """Different inputs should produce different masks."""
        result1 = mask_secret("secret_a")
        result2 = mask_secret("secret_b")
        assert result1 != result2

    def test_empty_string(self):
        result = mask_secret("")
        assert result.startswith("<masked:")
        assert len(result) > len("<masked:>")

    def test_numeric_input(self):
        """Should handle non-string inputs via str() conversion."""
        result = mask_secret(12345)
        assert result.startswith("<masked:")

    def test_zero_length(self):
        result = mask_secret("test", length=0)
        assert result == "<masked:>"


class TestParseRepoName:
    """Tests for the parse_repo_name utility function."""

    def test_basic_parsing(self):
        repo_data = {
            "owner": {"login": "entrius"},
            "name": "gittensor",
        }
        assert parse_repo_name(repo_data) == "entrius/gittensor"

    def test_lowercases_output(self):
        repo_data = {
            "owner": {"login": "OpenTensor"},
            "name": "BitTensor",
        }
        assert parse_repo_name(repo_data) == "opentensor/bittensor"

    def test_preserves_hyphens_and_underscores(self):
        repo_data = {
            "owner": {"login": "my-org"},
            "name": "my_repo-name",
        }
        assert parse_repo_name(repo_data) == "my-org/my_repo-name"

    def test_missing_owner_raises(self):
        with pytest.raises(KeyError):
            parse_repo_name({"name": "repo"})

    def test_missing_name_raises(self):
        with pytest.raises(KeyError):
            parse_repo_name({"owner": {"login": "org"}})
