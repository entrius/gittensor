"""Tests for inline test detection in Rust, Zig, and D source files."""

import pytest

from gittensor.constants import INLINE_TEST_EXTENSIONS
from gittensor.validator.utils.load_weights import TokenConfig, load_token_config
from gittensor.validator.utils.tree_sitter_scoring import has_inline_tests, parse_code


@pytest.fixture
def weights() -> TokenConfig:
    return load_token_config()


# -- Rust ------------------------------------------------------------------


def test_rust_cfg_test_module_detected(weights):
    lang = weights.get_language('rs')
    code = 'fn prod() -> i32 { 42 }\n#[cfg(test)]\nmod tests { fn t() {} }\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'rs') is True


def test_rust_test_fn_detected(weights):
    lang = weights.get_language('rs')
    code = 'fn prod() -> i32 { 42 }\n#[test]\nfn test_it() {}\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'rs') is True


def test_rust_tokio_test_detected(weights):
    lang = weights.get_language('rs')
    code = 'fn prod() {}\n#[tokio::test]\nasync fn test_it() {}\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'rs') is True


def test_rust_compound_cfg_detected(weights):
    lang = weights.get_language('rs')
    code = '#[cfg(all(test, feature = "x"))]\nmod tests { fn t() {} }\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'rs') is True


def test_rust_cfg_not_test_not_detected(weights):
    lang = weights.get_language('rs')
    code = '#[cfg(not(test))]\nmod prod_only { fn p() {} }\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'rs') is False


def test_rust_cfg_not_any_test_not_detected(weights):
    lang = weights.get_language('rs')
    code = '#[cfg(not(any(test, feature = "x")))]\nmod prod_only { fn p() {} }\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'rs') is False


def test_rust_cfg_all_not_feature_and_test_detected(weights):
    """test is NOT inside not() — only feature is negated."""
    lang = weights.get_language('rs')
    code = '#[cfg(all(not(feature = "x"), test))]\nmod tests { fn t() {} }\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'rs') is True


def test_rust_inner_attribute_cfg_test_detected(weights):
    """#![cfg(test)] inner attribute gates the entire module."""
    lang = weights.get_language('rs')
    code = '#![cfg(test)]\nfn test_helper() {}\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'rs') is True


def test_rust_doc_test_attribute_not_detected(weights):
    """#[doc(test(...))] is documentation config, not an inline test."""
    lang = weights.get_language('rs')
    code = '#[doc(test(no_crate_inject))]\nfn prod() {}\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'rs') is False


def test_rust_cfg_attr_not_detected(weights):
    """#[cfg_attr(test, inline)] is conditional compilation, not an inline test."""
    lang = weights.get_language('rs')
    code = '#[cfg_attr(test, inline)]\nfn prod() {}\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'rs') is False


def test_rust_double_negation_detected(weights):
    """not(not(test)) is semantically equivalent to cfg(test)."""
    lang = weights.get_language('rs')
    code = '#[cfg(not(not(test)))]\nmod tests { fn t() {} }\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'rs') is True


def test_rust_production_only_not_detected(weights):
    lang = weights.get_language('rs')
    code = 'fn prod() -> i32 { 42 }\nfn other() {}\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'rs') is False


# -- Zig ------------------------------------------------------------------


def test_zig_test_detected(weights):
    lang = weights.get_language('zig')
    code = 'fn add(a: i32, b: i32) i32 { return a + b; }\ntest "add" { }\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'zig') is True


def test_zig_production_only_not_detected(weights):
    lang = weights.get_language('zig')
    code = 'fn add(a: i32, b: i32) i32 { return a + b; }\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'zig') is False


# -- D ---------------------------------------------------------------------


def test_d_unittest_detected(weights):
    lang = weights.get_language('d')
    code = 'int add(int a, int b) { return a + b; }\nunittest { assert(add(1,2) == 3); }\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'd') is True


def test_d_production_only_not_detected(weights):
    lang = weights.get_language('d')
    code = 'int add(int a, int b) { return a + b; }\n'
    tree = parse_code(code, lang)
    assert has_inline_tests(tree, 'd') is False


# -- Unsupported / Constants -----------------------------------------------


def test_unsupported_extension_returns_false(weights):
    lang = weights.get_language('py')
    tree = parse_code('def foo(): pass', lang)
    assert has_inline_tests(tree, 'py') is False


def test_inline_test_extensions_constant():
    assert 'rs' in INLINE_TEST_EXTENSIONS
    assert 'zig' in INLINE_TEST_EXTENSIONS
    assert 'd' in INLINE_TEST_EXTENSIONS
    assert 'py' not in INLINE_TEST_EXTENSIONS
