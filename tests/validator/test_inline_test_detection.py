"""Tests for inline test detection in Rust, Zig, and D source files."""

from gittensor.constants import INLINE_TEST_EXTENSIONS
from gittensor.validator.utils.tree_sitter_scoring import has_inline_tests

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
