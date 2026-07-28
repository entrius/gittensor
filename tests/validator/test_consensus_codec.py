# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Tests for canonical preference quantization and structured basket validation."""

import pytest

from gittensor.validator.weight_consensus.codec import (
    U16_MAX,
    CodecError,
    canonicalize_prefs,
    validate_prefs,
)


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


class TestValidatePrefs:
    def test_valid_basket_returned_sorted(self):
        assert validate_prefs({'c/d': 100, 'a/b': 200}) == {'a/b': 200, 'c/d': 100}
        assert list(validate_prefs({'c/d': 1, 'a/b': 2})) == ['a/b', 'c/d']

    def test_lowercases_repo_names(self):
        assert validate_prefs({'Owner/Repo': 100}) == {'owner/repo': 100}

    def test_rejects_duplicates_after_lowercasing(self):
        assert validate_prefs({'a/b': 100, 'A/B': 200}) is None

    def test_drops_zero_weights_and_rejects_empty_result(self):
        assert validate_prefs({'a/b': 0, 'c/d': 100}) == {'c/d': 100}
        assert validate_prefs({'a/b': 0}) is None

    @pytest.mark.parametrize('name', ['not a repo', 'noslash', '/leading', 'UPPER CASE/x', ''])
    def test_rejects_malformed_names(self, name):
        assert validate_prefs({name: 100}) is None

    @pytest.mark.parametrize('weight', [-1, U16_MAX + 1, 1.5, '100', True, None])
    def test_rejects_non_u16_weights(self, weight):
        assert validate_prefs({'a/b': weight}) is None

    def test_rejects_too_many_repos(self):
        assert validate_prefs({f'a/repo{i}': 100 for i in range(11)}) is None

    def test_rejects_non_mapping_and_empty(self):
        assert validate_prefs(None) is None
        assert validate_prefs([('a/b', 100)]) is None
        assert validate_prefs({}) is None
