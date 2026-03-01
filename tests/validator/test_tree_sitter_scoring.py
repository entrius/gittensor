"""
Edge-case and robustness tests for tree_sitter_scoring.

Covers:
- Content normalization (None, non-str, empty, whitespace)
- Safe UTF-8 encode/decode (invalid surrogates, malformed bytes)
- parse_code with invalid or edge-case content
- collect_node_signatures depth limit and safe node text
- score_tree_diff with empty, None, invalid UTF-8 content
- calculate_token_score_from_file_changes with empty/invalid content
"""

import pytest

from gittensor.classes import FileChange
from gittensor.utils.github_api_tools import FileContentPair
from gittensor.validator.utils.load_weights import LanguageConfig, load_token_config
from gittensor.validator.utils.tree_sitter_scoring import (
    _normalize_content,
    _safe_content_byte_size,
    _safe_decode_node_text,
    _safe_encode_content,
    calculate_token_score_from_file_changes,
    collect_node_signatures,
    parse_code,
    score_tree_diff,
)


# =============================================================================
# _normalize_content
# =============================================================================


class TestNormalizeContent:
    def test_none_returns_none(self):
        assert _normalize_content(None) is None

    def test_empty_string_returns_none(self):
        assert _normalize_content('') is None

    def test_whitespace_only_returns_none(self):
        assert _normalize_content('   \n\t  ') is None

    def test_non_str_returns_none(self):
        assert _normalize_content(123) is None
        assert _normalize_content([]) is None
        assert _normalize_content(b'bytes') is None

    def test_normal_string_returns_stripped(self):
        assert _normalize_content('  def foo(): pass  ') == 'def foo(): pass'

    def test_single_line_no_whitespace_returns_same(self):
        assert _normalize_content('x=1') == 'x=1'


# =============================================================================
# _safe_encode_content
# =============================================================================


class TestSafeEncodeContent:
    def test_normal_string_returns_bytes(self):
        out = _safe_encode_content('hello')
        assert out is not None
        assert out == b'hello'

    def test_unicode_encodes(self):
        out = _safe_encode_content('café')
        assert out is not None
        assert out.decode('utf-8') == 'café'

    def test_invalid_surrogate_uses_replace(self):
        # Lone surrogate can cause encode error in strict mode; replace replaces it
        s = 'x\udc80y'  # invalid in UTF-8
        out = _safe_encode_content(s)
        assert out is not None
        assert b'x' in out and b'y' in out


# =============================================================================
# _safe_decode_node_text
# =============================================================================


class TestSafeDecodeNodeText:
    def test_none_returns_empty_string(self):
        assert _safe_decode_node_text(None) == ''

    def test_empty_bytes_returns_empty_string(self):
        assert _safe_decode_node_text(b'') == ''

    def test_valid_utf8_returns_string(self):
        assert _safe_decode_node_text(b'hello') == 'hello'

    def test_invalid_utf8_uses_replace(self):
        # Invalid UTF-8 byte sequence
        raw = b'hello\xff\xfe world'
        result = _safe_decode_node_text(raw)
        assert 'hello' in result
        assert 'world' in result


# =============================================================================
# _safe_content_byte_size
# =============================================================================


class TestSafeContentByteSize:
    def test_none_returns_zero(self):
        assert _safe_content_byte_size(None) == 0

    def test_non_str_returns_zero(self):
        assert _safe_content_byte_size(123) == 0
        assert _safe_content_byte_size([]) == 0

    def test_empty_string_returns_zero(self):
        assert _safe_content_byte_size('') == 0

    def test_ascii_returns_length(self):
        assert _safe_content_byte_size('abc') == 3

    def test_unicode_returns_utf8_byte_length(self):
        assert _safe_content_byte_size('café') == 5  # é is 2 bytes in UTF-8


# =============================================================================
# parse_code
# =============================================================================


class TestParseCodeEdgeCases:
    @pytest.fixture
    def weights(self):
        return load_token_config()

    def test_none_content_returns_none(self, weights):
        assert parse_code(None, 'py') is None

    def test_empty_string_returns_none(self, weights):
        assert parse_code('', 'py') is None

    def test_whitespace_only_returns_none(self, weights):
        assert parse_code('   \n  ', 'py') is None

    def test_non_str_content_returns_none(self, weights):
        assert parse_code(123, 'py') is None

    def test_valid_content_returns_tree(self, weights):
        # parse_code expects tree-sitter language name (e.g. 'python'), not extension
        tree = parse_code('def f(): pass', 'python')
        if tree is None:
            pytest.skip('python parser not available (tree_sitter_language_pack)')
        assert tree.root_node is not None

    def test_invalid_utf8_surrogate_does_not_raise(self, weights):
        # Content with invalid surrogate; encoder uses replace so parse may still run
        s = 'def f(): pass  \udc80'
        tree = parse_code(s, 'python')
        # Either None or a tree (parser may tolerate replacement char)
        assert tree is None or tree.root_node is not None

    def test_unknown_language_returns_none(self, weights):
        tree = parse_code('x = 1', 'nonexistent_lang_xyz')
        assert tree is None


# =============================================================================
# collect_node_signatures
# =============================================================================


class TestCollectNodeSignaturesEdgeCases:
    @pytest.fixture
    def weights(self):
        return load_token_config()

    def test_empty_tree_returns_empty_counter(self, weights):
        tree = parse_code('', 'python')
        if tree is None:
            pytest.skip('parse_code returned None for empty string')
        sigs = collect_node_signatures(tree, weights)
        assert len(sigs) == 0

    def test_small_max_depth_limits_walk(self, weights):
        # Deeply nested code; max_depth=1 should truncate and not crash
        deep = 'def a():\n  def b():\n    def c():\n      def d():\n        pass'
        tree = parse_code(deep, 'python')
        if tree is None:
            pytest.skip('python parser not available')
        sigs = collect_node_signatures(tree, weights, max_depth=1)
        # Should complete without stack overflow; may have few or zero structural nodes at depth 0
        assert isinstance(sigs, type(__import__('collections').Counter()))


# =============================================================================
# score_tree_diff
# =============================================================================


class TestScoreTreeDiffEdgeCases:
    @pytest.fixture
    def weights(self):
        return load_token_config()

    def test_both_none_returns_zero_breakdown(self, weights):
        b = score_tree_diff(None, None, 'py', weights)
        assert b.total_score == 0
        assert b.added_count == 0
        assert b.deleted_count == 0

    def test_old_none_new_empty_returns_zero(self, weights):
        b = score_tree_diff(None, '', 'py', weights)
        assert b.total_score == 0

    def test_old_none_new_whitespace_returns_zero(self, weights):
        b = score_tree_diff(None, '   \n  ', 'py', weights)
        assert b.total_score == 0

    def test_new_empty_old_valid_returns_zero_additions(self, weights):
        old = 'def f(): pass'
        b = score_tree_diff(old, '', 'py', weights)
        # Deletions only (old content removed)
        assert b.deleted_count >= 0
        assert b.added_count == 0

    def test_invalid_utf8_in_new_content_does_not_raise(self, weights):
        old = 'def f(): pass'
        new = 'def g(): pass  \udc80'
        b = score_tree_diff(old, new, 'py', weights)
        assert b.total_score >= 0

    def test_identical_content_scores_zero(self, weights):
        content = 'def foo():\n    return 1'
        b = score_tree_diff(content, content, 'py', weights)
        assert b.total_score == 0
        assert b.added_count == 0
        assert b.deleted_count == 0


# =============================================================================
# calculate_token_score_from_file_changes (empty / invalid content)
# =============================================================================


class TestCalculateTokenScoreEdgeCases:
    """Edge cases for full pipeline: empty new content, non-str content."""

    @pytest.fixture
    def weights(self):
        return load_token_config()

    @pytest.fixture
    def programming_languages(self):
        return {'py': LanguageConfig(weight=1.0, language='python')}

    def test_empty_new_content_skipped_as_empty(self, weights, programming_languages):
        fc = FileChange(
            pr_number=1,
            repository_full_name='o/r',
            filename='src/foo.py',
            changes=1,
            additions=1,
            deletions=0,
            status='modified',
        )
        file_contents = {
            'src/foo.py': FileContentPair(old_content='def x(): pass', new_content=''),
        }
        result = calculate_token_score_from_file_changes(
            [fc], file_contents, weights, programming_languages
        )
        assert result.total_score == 0
        assert len(result.file_results) == 1
        assert result.file_results[0].scoring_method == 'skipped-empty'

    def test_whitespace_only_new_content_skipped(self, weights, programming_languages):
        fc = FileChange(
            pr_number=1,
            repository_full_name='o/r',
            filename='bar.py',
            changes=2,
            additions=2,
            deletions=0,
            status='modified',
        )
        file_contents = {
            'bar.py': FileContentPair(old_content='a=1', new_content='   \n  '),
        }
        result = calculate_token_score_from_file_changes(
            [fc], file_contents, weights, programming_languages
        )
        assert result.total_score == 0
        assert result.file_results[0].scoring_method == 'skipped-empty'
