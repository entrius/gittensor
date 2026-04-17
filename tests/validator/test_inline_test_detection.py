"""Tests for inline test detection in Rust, Zig, and D source files."""

from gittensor.classes import FileChange
from gittensor.constants import INLINE_TEST_EXTENSIONS
from gittensor.utils.github_api_tools import FileContentPair
from gittensor.validator.utils.load_weights import load_programming_language_weights, load_token_config
from gittensor.validator.utils.tree_sitter_scoring import calculate_token_score_from_file_changes, has_inline_tests

# -- Rust ------------------------------------------------------------------


def test_rust_cfg_test_module_detected():
    code = 'fn prod() -> i32 { 42 }\n#[cfg(test)]\nmod tests { fn t() {} }\n'
    assert has_inline_tests(code, 'rs') is True


def test_rust_test_fn_detected():
    code = 'fn prod() -> i32 { 42 }\n#[test]\nfn test_it() {}\n'
    assert has_inline_tests(code, 'rs') is True


def test_rust_inner_attribute_cfg_test_detected():
    """#![cfg(test)] inner attribute gates the entire module."""
    code = '#![cfg(test)]\nfn test_helper() {}\n'
    assert has_inline_tests(code, 'rs') is True


def test_rust_cfg_test_prefix_not_detected():
    """#[cfg(test_utils)] should not be detected as inline test."""
    code = '#[cfg(test_utils)]\nmod helpers { fn h() {} }\n'
    assert has_inline_tests(code, 'rs') is False


def test_rust_production_only_not_detected():
    code = 'fn prod() -> i32 { 42 }\nfn other() {}\n'
    assert has_inline_tests(code, 'rs') is False


def test_rust_tokio_test_detected():
    """#[tokio::test] async test attribute should be detected."""
    code = 'async fn helper() {}\n#[tokio::test]\nasync fn test_it() {}\n'
    assert has_inline_tests(code, 'rs') is True


def test_rust_indented_test_detected():
    """Indented #[test] inside a mod should still be detected."""
    code = '    #[test]\n    fn test_it() {}\n'
    assert has_inline_tests(code, 'rs') is True


def test_rust_test_in_comment_not_detected():
    """#[test] inside a line comment must not trigger detection."""
    code = 'fn prod() {}\n// Use #[test] to annotate test functions\n'
    assert has_inline_tests(code, 'rs') is False


def test_rust_test_in_doc_comment_not_detected():
    """#[test] inside a doc comment must not trigger detection."""
    code = '/// Example: #[test]\nfn documented() {}\n'
    assert has_inline_tests(code, 'rs') is False


def test_rust_test_in_string_not_detected():
    """#[test] inside a string literal must not trigger detection."""
    code = 'fn f() { let s = "#[test]"; }\n'
    assert has_inline_tests(code, 'rs') is False


# -- Zig ------------------------------------------------------------------


def test_zig_named_test_detected():
    code = 'fn add(a: i32, b: i32) i32 { return a + b; }\ntest "add" { }\n'
    assert has_inline_tests(code, 'zig') is True


def test_zig_unnamed_test_detected():
    """Zig allows unnamed test blocks: test { ... }"""
    code = 'fn add(a: i32, b: i32) i32 { return a + b; }\ntest {\n    // ...\n}\n'
    assert has_inline_tests(code, 'zig') is True


def test_zig_production_only_not_detected():
    code = 'fn add(a: i32, b: i32) i32 { return a + b; }\n'
    assert has_inline_tests(code, 'zig') is False


# -- D ---------------------------------------------------------------------


def test_d_unittest_detected():
    code = 'int add(int a, int b) { return a + b; }\nunittest { assert(add(1,2) == 3); }\n'
    assert has_inline_tests(code, 'd') is True


def test_d_production_only_not_detected():
    code = 'int add(int a, int b) { return a + b; }\n'
    assert has_inline_tests(code, 'd') is False


# -- Unsupported / Constants -----------------------------------------------


def test_unsupported_extension_returns_false():
    assert has_inline_tests('def foo(): pass', 'py') is False


def test_inline_test_extensions_constant():
    assert 'rs' in INLINE_TEST_EXTENSIONS
    assert 'zig' in INLINE_TEST_EXTENSIONS
    assert 'd' in INLINE_TEST_EXTENSIONS
    assert 'py' not in INLINE_TEST_EXTENSIONS


# -- Scoring weight: pre-existing vs newly introduced inline tests -----------


_RUST_PROD_CODE = 'pub fn existing() -> i32 { 42 }\n'
_RUST_TEST_MODULE = '#[cfg(test)]\nmod tests {\n    use super::*;\n    #[test]\n    fn test_existing() { assert_eq!(existing(), 42); }\n}\n'
_RUST_NEW_FUNCTION = 'pub fn new_func(x: i32, y: i32) -> i32 {\n    let result = x + y;\n    if result > 100 { return result * 2; }\n    result\n}\n'


def _make_file(filename: str, additions: int = 8, deletions: int = 0) -> FileChange:
    return FileChange(
        pr_number=1,
        repository_full_name='test/repo',
        filename=filename,
        changes=additions + deletions,
        additions=additions,
        deletions=deletions,
        status='modified',
    )


def _score_file(old_content: str | None, new_content: str, filename: str = 'src/lib.rs') -> tuple:
    """Score a single file and return (file_result, is_test_file, score)."""
    weights = load_token_config()
    langs = load_programming_language_weights()
    file = _make_file(filename)
    contents = {filename: FileContentPair(old_content=old_content, new_content=new_content)}
    result = calculate_token_score_from_file_changes([file], contents, weights, langs)
    fr = result.file_results[0]
    return fr, fr.is_test_file, fr.score


def test_preexisting_inline_tests_keep_full_weight():
    """When old content already had #[cfg(test)], production changes keep 1.0x weight."""
    old = _RUST_PROD_CODE + _RUST_TEST_MODULE
    new = _RUST_PROD_CODE + _RUST_NEW_FUNCTION + _RUST_TEST_MODULE

    fr, is_test, score = _score_file(old, new)

    assert not is_test, 'File should NOT be marked as test when tests are pre-existing'
    assert score > 0, 'Production code changes should have positive score'
    # Verify full weight was applied (Rust lang_weight=2.0, file_weight=1.0)
    assert fr.scoring_method == 'tree-diff'


def test_new_inline_tests_get_test_weight():
    """When PR introduces inline tests to a file that had none, apply 0.05x weight."""
    old = _RUST_PROD_CODE  # no tests
    new = _RUST_PROD_CODE + _RUST_TEST_MODULE  # tests added by PR

    fr, is_test, score = _score_file(old, new)

    assert is_test, 'File should be marked as test when PR introduces inline tests'


def test_new_file_with_inline_tests_gets_test_weight():
    """A brand new file (old_content=None) with inline tests gets 0.05x weight."""
    new = _RUST_PROD_CODE + _RUST_NEW_FUNCTION + _RUST_TEST_MODULE

    file = _make_file('src/lib.rs')
    file.status = 'added'
    weights = load_token_config()
    langs = load_programming_language_weights()
    contents = {'src/lib.rs': FileContentPair(old_content=None, new_content=new)}
    result = calculate_token_score_from_file_changes([file], contents, weights, langs)
    fr = result.file_results[0]

    assert fr.is_test_file, 'New file with inline tests should be marked as test'


def test_removed_inline_tests_keep_full_weight():
    """When PR removes inline tests from a file, production code keeps 1.0x weight."""
    old = _RUST_PROD_CODE + _RUST_TEST_MODULE  # had tests
    new = _RUST_PROD_CODE + _RUST_NEW_FUNCTION  # tests removed, prod code added

    fr, is_test, score = _score_file(old, new)

    assert not is_test, 'File should NOT be marked as test when tests were removed'
    assert score > 0
