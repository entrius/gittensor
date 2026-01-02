# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for token-based scoring using tree-sitter.

Run tests:
    pytest tests/validator/test_token_scoring.py -v
"""

import pytest

from gittensor.validator.utils.load_weights import (
    TokenWeights,
    get_documentation_extensions,
    get_supported_extensions,
    load_token_weights,
)


class TestTokenWeightsLoading:
    """Test loading token weights from JSON configuration."""

    def test_load_token_weights_returns_valid_config(self):
        """load_token_weights returns a fully populated TokenWeights instance."""
        weights = load_token_weights()
        assert isinstance(weights, TokenWeights)
        assert len(weights.structural_bonus) > 0
        assert len(weights.leaf_tokens) > 0
        assert len(weights.extension_to_language) > 0
        assert len(weights.documentation_extensions) > 0


class TestTokenWeightsMethods:
    """Test TokenWeights class methods."""

    @pytest.fixture
    def weights(self) -> TokenWeights:
        return load_token_weights()

    def test_get_weights_returns_values_or_zero(self, weights):
        """Weight getters return positive values for known types, zero for unknown."""
        assert weights.get_structural_weight('function_definition') > 0
        assert weights.get_structural_weight('unknown_xyz') == 0.0
        assert weights.get_leaf_weight('identifier') > 0
        assert weights.get_leaf_weight('unknown_xyz') == 0.0

    def test_get_language(self, weights):
        """get_language maps extensions to tree-sitter language names."""
        assert weights.get_language('py') == 'python'
        assert weights.get_language('.py') == 'python'
        assert weights.get_language('PY') == 'python'
        assert weights.get_language('unknown') is None

    def test_supports_tree_sitter(self, weights):
        """supports_tree_sitter distinguishes code from documentation files."""
        assert weights.supports_tree_sitter('py') is True
        assert weights.supports_tree_sitter('md') is False
        assert weights.supports_tree_sitter('unknown') is False


class TestHelperFunctions:
    """Test module-level helper functions."""

    def test_get_supported_extensions(self):
        """get_supported_extensions returns code file extensions only."""
        extensions = get_supported_extensions()
        assert 'py' in extensions
        assert 'js' in extensions
        assert 'md' not in extensions

    def test_get_documentation_extensions(self):
        """get_documentation_extensions returns documentation file types."""
        extensions = get_documentation_extensions()
        assert 'md' in extensions
        assert 'json' in extensions


class TestWeightValues:
    """Test that weight values are sensible."""

    def test_weight_hierarchy(self):
        """Structural weights follow expected hierarchy."""
        weights = load_token_weights()
        assert weights.get_structural_weight('class_definition') >= weights.get_structural_weight('function_definition')
        assert weights.get_structural_weight('function_definition') > weights.get_structural_weight('assignment')
        assert weights.get_leaf_weight('identifier') > weights.get_leaf_weight('integer')

    def test_comments_have_zero_weight(self):
        """Comment types are explicitly zero-weighted."""
        weights = load_token_weights()
        assert weights.get_leaf_weight('comment') == 0.0
        assert weights.get_leaf_weight('line_comment') == 0.0
        assert weights.get_leaf_weight('block_comment') == 0.0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
