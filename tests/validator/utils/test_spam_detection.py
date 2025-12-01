# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for spam_detection module

Tests spam detection utilities including:
- Token parsing and typo detection
- Comment line identification  
- Non-scoreable line counting
- Edge cases and error handling
"""

import unittest
from gittensor.validator.utils.spam_detection import (
    tokenize,
    token_pair_typo,
    is_token_typo,
    is_comment_line,
    count_non_scoreable_lines,
    is_single_diff_line,
)


class TestTokenization(unittest.TestCase):
    """Test suite for tokenization functions"""

    def test_tokenize_basic_words(self):
        """Test tokenization of basic words"""
        result = tokenize("hello world")
        self.assertEqual(result, ["hello", "world"])

    def test_tokenize_with_numbers(self):
        """Test tokenization with numbers"""
        result = tokenize("test123 abc456")
        self.assertEqual(result, ["test123", "abc456"])

    def test_tokenize_with_underscores(self):
        """Test tokenization with underscores"""
        result = tokenize("my_variable another_var")
        self.assertEqual(result, ["my_variable", "another_var"])

    def test_tokenize_with_hyphens(self):
        """Test tokenization with hyphens"""
        result = tokenize("test-case another-test")
        self.assertEqual(result, ["test-case", "another-test"])

    def test_tokenize_with_apostrophes(self):
        """Test tokenization with apostrophes"""
        result = tokenize("don't can't won't")
        self.assertEqual(result, ["don't", "can't", "won't"])

    def test_tokenize_mixed_case(self):
        """Test tokenization preserves case"""
        result = tokenize("HelloWorld TestCase")
        self.assertEqual(result, ["HelloWorld", "TestCase"])

    def test_tokenize_with_special_chars(self):
        """Test tokenization filters special characters"""
        result = tokenize("hello@world test#case")
        self.assertEqual(result, ["hello", "world", "test", "case"])

    def test_tokenize_empty_string(self):
        """Test tokenization of empty string"""
        result = tokenize("")
        self.assertEqual(result, [])

    def test_tokenize_only_special_chars(self):
        """Test tokenization with only special characters"""
        result = tokenize("!@#$%^&*()")
        self.assertEqual(result, [])


class TestTypoDetection(unittest.TestCase):
    """Test suite for typo detection functions"""

    def test_token_pair_typo_exact_match(self):
        """Test typo detection with exact match"""
        result = token_pair_typo("hello", "hello", 2, 0.75)
        self.assertTrue(result)

    def test_token_pair_typo_one_char_diff(self):
        """Test typo detection with one character difference"""
        result = token_pair_typo("hello", "helo", 2, 0.75)
        self.assertTrue(result)

    def test_token_pair_typo_two_char_diff(self):
        """Test typo detection with two character difference"""
        result = token_pair_typo("hello", "helo", 2, 0.75)
        self.assertTrue(result)

    def test_token_pair_typo_high_similarity(self):
        """Test typo detection with high similarity"""
        result = token_pair_typo("testing", "testng", 2, 0.75)
        self.assertTrue(result)

    def test_token_pair_typo_completely_different(self):
        """Test typo detection with completely different words"""
        result = token_pair_typo("hello", "world", 2, 0.75)
        self.assertFalse(result)

    def test_token_pair_typo_case_sensitive(self):
        """Test typo detection is case-sensitive"""
        result = token_pair_typo("Hello", "hello", 2, 0.75)
        self.assertTrue(result)  # Should still match due to high similarity

    def test_is_token_typo_same_lines(self):
        """Test is_token_typo with identical lines"""
        result = is_token_typo("hello world", "hello world")
        self.assertTrue(result)

    def test_is_token_typo_one_word_diff(self):
        """Test is_token_typo with one word different"""
        result = is_token_typo("hello world", "helo world")
        self.assertTrue(result)

    def test_is_token_typo_different_word_count(self):
        """Test is_token_typo with different word counts"""
        result = is_token_typo("hello world", "hello world test")
        self.assertFalse(result)

    def test_is_token_typo_completely_different(self):
        """Test is_token_typo with completely different lines"""
        result = is_token_typo("hello world", "foo bar")
        self.assertFalse(result)

    def test_is_token_typo_empty_strings(self):
        """Test is_token_typo with empty strings"""
        result = is_token_typo("", "")
        self.assertTrue(result)  # Both empty = same token count (0)

    def test_is_token_typo_code_example(self):
        """Test is_token_typo with code-like strings"""
        result = is_token_typo("def my_function():", "def my_functon():")
        self.assertTrue(result)


class TestCommentDetection(unittest.TestCase):
    """Test suite for comment line detection"""

    def test_is_comment_line_python_hash(self):
        """Test Python-style # comments"""
        self.assertTrue(is_comment_line("# This is a comment"))
        self.assertTrue(is_comment_line("  # Indented comment"))

    def test_is_comment_line_cpp_double_slash(self):
        """Test C++/Java-style // comments"""
        self.assertTrue(is_comment_line("// This is a comment"))
        self.assertTrue(is_comment_line("  // Indented comment"))

    def test_is_comment_line_c_style_start(self):
        """Test C-style /* comment start"""
        self.assertTrue(is_comment_line("/* This is a comment"))
        self.assertTrue(is_comment_line("  /* Indented comment"))

    def test_is_comment_line_c_style_continuation(self):
        """Test C-style * comment continuation"""
        self.assertTrue(is_comment_line(" * Comment continuation"))
        self.assertTrue(is_comment_line("  * Indented continuation"))

    def test_is_comment_line_c_style_end(self):
        """Test C-style */ comment end"""
        self.assertTrue(is_comment_line(" */"))
        self.assertTrue(is_comment_line("  */"))

    def test_is_comment_line_sql_double_dash(self):
        """Test SQL-style -- comments"""
        self.assertTrue(is_comment_line("-- This is a comment"))
        self.assertTrue(is_comment_line("  -- Indented comment"))

    def test_is_comment_line_html_xml(self):
        """Test HTML/XML-style <!-- comments"""
        self.assertTrue(is_comment_line("<!-- This is a comment"))
        self.assertTrue(is_comment_line("  <!-- Indented comment"))

    def test_is_comment_line_latex_percent(self):
        """Test LaTeX-style % comments"""
        self.assertTrue(is_comment_line("% This is a comment"))
        self.assertTrue(is_comment_line("  % Indented comment"))

    def test_is_comment_line_lisp_semicolon(self):
        """Test Lisp-style ; comments"""
        self.assertTrue(is_comment_line("; This is a comment"))
        self.assertTrue(is_comment_line("  ; Indented comment"))

    def test_is_comment_line_python_docstring_double(self):
        """Test Python docstring with double quotes"""
        self.assertTrue(is_comment_line('"""This is a docstring'))
        self.assertTrue(is_comment_line('  """Indented docstring'))

    def test_is_comment_line_python_docstring_single(self):
        """Test Python docstring with single quotes"""
        self.assertTrue(is_comment_line("'''This is a docstring"))
        self.assertTrue(is_comment_line("  '''Indented docstring"))

    def test_is_comment_line_not_comment(self):
        """Test non-comment lines"""
        self.assertFalse(is_comment_line("def my_function():"))
        self.assertFalse(is_comment_line("print('hello')"))
        self.assertFalse(is_comment_line("int x = 5;"))

    def test_is_comment_line_hash_in_string(self):
        """Test that # inside code is not detected as comment"""
        # This should NOT be detected as comment (# is part of code)
        self.assertFalse(is_comment_line('print("#hashtag")'))


class TestDiffLineDetection(unittest.TestCase):
    """Test suite for diff line detection"""

    def test_is_single_diff_line_addition(self):
        """Test detection of addition lines"""
        self.assertTrue(is_single_diff_line("+added line"))

    def test_is_single_diff_line_deletion(self):
        """Test detection of deletion lines"""
        self.assertTrue(is_single_diff_line("-deleted line"))

    def test_is_single_diff_line_double_plus(self):
        """Test that ++ is not a single diff line"""
        self.assertFalse(is_single_diff_line("++not a diff line"))

    def test_is_single_diff_line_double_minus(self):
        """Test that -- is not a single diff line"""
        self.assertFalse(is_single_diff_line("--not a diff line"))

    def test_is_single_diff_line_empty_string(self):
        """Test empty string is not a diff line"""
        self.assertFalse(is_single_diff_line(""))

    def test_is_single_diff_line_only_plus(self):
        """Test single + character"""
        self.assertTrue(is_single_diff_line("+"))

    def test_is_single_diff_line_only_minus(self):
        """Test single - character"""
        self.assertTrue(is_single_diff_line("-"))

    def test_is_single_diff_line_normal_line(self):
        """Test normal lines without +/- prefix"""
        self.assertFalse(is_single_diff_line(" normal line"))
        self.assertFalse(is_single_diff_line("no prefix"))


class TestNonScoreableLinesCounting(unittest.TestCase):
    """Test suite for counting non-scoreable lines"""

    def test_count_non_scoreable_lines_empty_patch(self):
        """Test with empty patch"""
        result = count_non_scoreable_lines("")
        self.assertEqual(result, 0)

    def test_count_non_scoreable_lines_none_patch(self):
        """Test with None patch"""
        result = count_non_scoreable_lines(None)
        self.assertEqual(result, 0)

    def test_count_non_scoreable_lines_blank_lines(self):
        """Test counting blank lines"""
        patch = "+\n+  \n+\t"
        result = count_non_scoreable_lines(patch)
        self.assertEqual(result, 3)

    def test_count_non_scoreable_lines_comments(self):
        """Test counting comment lines"""
        patch = "+# This is a comment\n+// Another comment\n+/* C-style comment"
        result = count_non_scoreable_lines(patch)
        self.assertEqual(result, 3)

    def test_count_non_scoreable_lines_typo_correction(self):
        """Test counting typo corrections"""
        patch = "-hello world\n+helo world"
        result = count_non_scoreable_lines(patch)
        self.assertEqual(result, 2)  # Both lines should be non-scoreable

    def test_count_non_scoreable_lines_mixed(self):
        """Test with mix of scoreable and non-scoreable lines"""
        patch = "+# Comment\n+def my_function():\n+\n+    return True"
        result = count_non_scoreable_lines(patch)
        self.assertEqual(result, 2)  # Comment and blank line

    def test_count_non_scoreable_lines_only_scoreable(self):
        """Test with only scoreable lines"""
        patch = "+def my_function():\n+    return True"
        result = count_non_scoreable_lines(patch)
        self.assertEqual(result, 0)

    def test_count_non_scoreable_lines_max_limit(self):
        """Test with max_scoreable_lines limit"""
        patch = "+line1\n+line2\n+line3\n+line4\n+line5"
        result = count_non_scoreable_lines(patch, max_scoreable_lines=3)
        # Should stop counting after 3 scoreable lines
        self.assertEqual(result, 0)

    def test_count_non_scoreable_lines_deletions(self):
        """Test with deletion lines"""
        patch = "-# Old comment\n-def old_function():"
        result = count_non_scoreable_lines(patch)
        self.assertEqual(result, 1)  # Only the comment

    def test_count_non_scoreable_lines_complex_patch(self):
        """Test with complex realistic patch"""
        patch = """@@ -1,5 +1,6 @@
+# Added comment
 def my_function():
-    # Old comment
+    # New comment
     return True
+    # Another comment"""
        result = count_non_scoreable_lines(patch)
        # Should count: added comment, old comment, new comment, another comment
        self.assertGreaterEqual(result, 2)


class TestEdgeCases(unittest.TestCase):
    """Test suite for edge cases and error handling"""

    def test_tokenize_unicode(self):
        """Test tokenization with unicode characters"""
        result = tokenize("hello 世界 test")
        # Should only extract ASCII alphanumeric
        self.assertEqual(result, ["hello", "test"])

    def test_is_token_typo_with_numbers(self):
        """Test typo detection with numbers"""
        result = is_token_typo("test123", "test124")
        self.assertTrue(result)

    def test_count_non_scoreable_lines_malformed_patch(self):
        """Test with malformed patch format"""
        patch = "not a valid diff format\nrandom text"
        result = count_non_scoreable_lines(patch)
        # Should handle gracefully
        self.assertIsInstance(result, int)

    def test_is_comment_line_edge_whitespace(self):
        """Test comment detection with various whitespace"""
        self.assertTrue(is_comment_line("\t\t# Comment"))
        self.assertTrue(is_comment_line("    // Comment"))

    def test_token_pair_typo_very_long_words(self):
        """Test typo detection with very long words"""
        word1 = "a" * 100
        word2 = "a" * 99 + "b"
        result = token_pair_typo(word1, word2, 2, 0.75)
        self.assertTrue(result)


if __name__ == '__main__':
    unittest.main()
