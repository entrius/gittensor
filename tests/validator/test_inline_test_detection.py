"""Tests for inline test detection in Rust, Zig, and D source files."""

from gittensor.constants import INLINE_TEST_EXTENSIONS
from gittensor.validator.utils.load_weights import load_token_config
from gittensor.validator.utils.tree_sitter_scoring import (
    _inline_test_line_ranges,
    _score_tree_diff_split,
    has_inline_tests,
)

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


# -- Line range extraction -------------------------------------------------


def test_line_ranges_empty_for_non_inline_test_ext():
    content = 'def f():\n    pass\n'
    assert _inline_test_line_ranges(content, 'py') == []


def test_line_ranges_empty_for_production_only_rust():
    content = 'fn f() -> i32 { 42 }\nfn g() {}\n'
    assert _inline_test_line_ranges(content, 'rs') == []


def test_line_ranges_covers_cfg_test_module():
    content = 'fn prod() {}\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() {}\n}\ntrailing\n'
    ranges = _inline_test_line_ranges(content, 'rs')
    assert ranges == [(1, 5)]


def test_line_ranges_covers_standalone_test_fn():
    content = 'fn prod() {}\n#[test]\nfn test_a() {\n    assert!(true);\n}\nfn more() {}\n'
    ranges = _inline_test_line_ranges(content, 'rs')
    assert ranges == [(1, 4)]


def test_line_ranges_inner_attribute_gates_whole_file():
    content = '#![cfg(test)]\nfn helper() {}\nfn more() {}\n'
    ranges = _inline_test_line_ranges(content, 'rs')
    assert ranges == [(0, 3)]


def test_line_ranges_tokio_test_detected():
    content = 'fn prod() {}\n#[tokio::test]\nasync fn t() {\n    assert!(true);\n}\n'
    ranges = _inline_test_line_ranges(content, 'rs')
    assert ranges == [(1, 4)]


# -- Per-block scoring split ----------------------------------------------


def test_split_scores_production_and_test_subtrees_independently():
    """Issue #631: production edits in an inline-test-containing file must not be demoted."""
    weights = load_token_config()
    old = 'pub fn parse(x: &str) -> i32 { x.len() as i32 }\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() { assert_eq!(parse("hi"), 2); }\n}\n'
    new = 'pub fn parse(x: &str) -> i32 { (x.len() * 2) as i32 }\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() { assert_eq!(parse("hi"), 4); }\n}\n'
    prod_bd, test_bd = _score_tree_diff_split(old, new, 'rs', weights)
    assert prod_bd.total_score > 0, 'Production edits should contribute positive score'
    assert test_bd.total_score >= 0
    assert prod_bd.added_count > 0 or prod_bd.deleted_count > 0


def test_split_empty_when_no_inline_tests():
    """Files without inline tests should yield all-production, zero-test breakdown."""
    weights = load_token_config()
    new = 'pub fn f() -> i32 { 1 }\npub fn g() -> i32 { 2 }\n'
    prod_bd, test_bd = _score_tree_diff_split(None, new, 'rs', weights)
    assert prod_bd.total_score > 0
    assert test_bd.total_score == 0
    assert test_bd.added_count == 0 and test_bd.deleted_count == 0


def test_split_routes_test_only_changes_to_test_bucket():
    """A diff that only touches lines inside an inline-test block must score in TEST, not SOURCE."""
    weights = load_token_config()
    old = 'pub fn prod() -> i32 { 42 }\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() { assert_eq!(prod(), 42); }\n}\n'
    new = 'pub fn prod() -> i32 { 42 }\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() { assert_eq!(prod(), 42); }\n    #[test]\n    fn t2() { assert!(prod() > 0); }\n}\n'
    prod_bd, test_bd = _score_tree_diff_split(old, new, 'rs', weights)
    assert test_bd.total_score > 0, 'New test function should count in TEST bucket'
    assert prod_bd.added_count == 0 and prod_bd.deleted_count == 0, 'Production subtree unchanged'
