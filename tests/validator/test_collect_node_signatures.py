"""Unit tests for collect_node_signatures cursor-based AST traversal."""

import pytest

from gittensor.constants import COMMENT_NODE_TYPES
from gittensor.validator.utils.load_weights import TokenConfig, load_token_config
from gittensor.validator.utils.tree_sitter_scoring import (
    collect_node_signatures,
    parse_code,
)


@pytest.fixture
def weights() -> TokenConfig:
    return load_token_config()


def _signatures(content: str, language: str, weights: TokenConfig):
    tree = parse_code(content, language)
    assert tree is not None, f'parser missing for {language}'
    return collect_node_signatures(tree, weights)


def test_comments_excluded_entirely(weights):
    """Comment nodes and their children must not appear in the signature multiset."""
    content = 'def foo():\n    # leaf token inside comment\n    return 1\n'
    sigs = _signatures(content, 'python', weights)

    for sig in sigs:
        assert sig[1] not in COMMENT_NODE_TYPES, f'comment node leaked into signatures: {sig}'
        if len(sig) == 3:
            assert b'leaf token inside comment' not in sig[2]


def test_structural_and_leaf_signatures_present(weights):
    """A simple function should produce both structural and leaf signatures."""
    content = 'def foo():\n    return 1\n'
    sigs = _signatures(content, 'python', weights)

    assert ('structural', 'function_definition') in sigs
    assert ('leaf', 'identifier', b'foo') in sigs
    assert ('leaf', 'integer', b'1') in sigs


def test_duplicate_structures_counted(weights):
    """Counter must track multiplicity for repeated structural nodes."""
    content = 'def a():\n    return 1\n\ndef b():\n    return 2\n'
    sigs = _signatures(content, 'python', weights)

    assert sigs[('structural', 'function_definition')] == 2
    assert sigs[('leaf', 'identifier', b'a')] == 1
    assert sigs[('leaf', 'identifier', b'b')] == 1


def test_rust_signatures(weights):
    """Rust traversal collects structural nodes and identifiers, skips comments."""
    content = 'fn run() {\n    // note\n    let x = 1;\n    let y = 2;\n}\n'
    sigs = _signatures(content, 'rust', weights)

    assert sigs[('structural', 'let_declaration')] == 2
    assert ('leaf', 'identifier', b'x') in sigs
    assert ('leaf', 'identifier', b'y') in sigs
    for sig in sigs:
        assert sig[1] not in COMMENT_NODE_TYPES


def test_bash_signatures(weights):
    """Bash traversal handles flat scripts without recursion-stack issues."""
    content = '#!/bin/bash\n# greet\necho hello\necho world\n'
    sigs = _signatures(content, 'bash', weights)

    assert ('leaf', 'word', b'echo') in sigs
    assert sigs[('leaf', 'word', b'echo')] == 2
    for sig in sigs:
        assert sig[1] not in COMMENT_NODE_TYPES
