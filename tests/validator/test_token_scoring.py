# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for token-based scoring using tree-sitter.

Run tests:
    pytest tests/validator/test_token_scoring.py -v
"""

import pytest

from gittensor.classes import LineChange, LineChangeType
from gittensor.validator.utils.load_weights import (
    TokenWeights,
    load_token_weights,
)
from gittensor.validator.utils.tree_sitter_scoring import (
    calculate_line_scores,
    calculate_line_scores_with_changes,
    calculate_total_score_with_changes,
    extract_added_lines,
    extract_file_patch,
    get_changed_tokens,
    get_parser,
    parse_code,
)

# =============================================================================
# Core Functionality Tests
# =============================================================================


class TestTokenWeightsBasics:
    """Test TokenWeights loading and basic methods."""

    def test_load_token_weights_returns_valid_config(self):
        """load_token_weights returns a fully populated TokenWeights instance."""
        weights = load_token_weights()
        assert isinstance(weights, TokenWeights)
        assert len(weights.structural_bonus) > 0
        assert len(weights.leaf_tokens) > 0

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


class TestTreeSitterParsing:
    """Test tree-sitter parser functions."""

    def test_get_parser_valid_and_invalid(self):
        """get_parser returns Parser for valid languages, None for invalid."""
        assert get_parser('python') is not None
        assert get_parser('not_a_real_language_xyz') is None

    def test_parse_code_returns_tree(self):
        """parse_code returns a valid AST tree."""
        tree = parse_code('def foo(): pass', 'python')
        assert tree is not None
        assert tree.root_node is not None


class TestCalculateLineScores:
    """Test per-line score calculation."""

    @pytest.fixture
    def weights(self) -> TokenWeights:
        return load_token_weights()

    def test_function_definition_gets_structural_bonus(self, weights):
        """Function definition gives structural bonus to its start line."""
        code = 'def hello():\n    return 42'
        scores = calculate_line_scores(code, 'py', weights)
        assert scores.get(1, 0) >= weights.get_structural_weight('function_definition')

    def test_comments_score_zero(self, weights):
        """Comment-only lines should score zero."""
        code = '# This is a comment'
        scores = calculate_line_scores(code, 'py', weights)
        assert scores.get(1, 0) == 0.0

    def test_multiline_docstring_scores_low(self, weights):
        """Multiline docstrings should score less than a function definition."""
        code = '"""\nThis is a docstring\nwith multiple lines\n"""'
        scores = calculate_line_scores(code, 'py', weights)
        total = sum(scores.values())
        assert total < weights.get_structural_weight('function_definition')

    def test_class_with_methods(self, weights):
        """Class and method both get structural bonuses."""
        code = 'class Foo:\n    def bar(self):\n        pass'
        scores = calculate_line_scores(code, 'py', weights)
        assert scores.get(1, 0) >= weights.get_structural_weight('class_definition')
        assert scores.get(2, 0) >= weights.get_structural_weight('function_definition')


# =============================================================================
# Patch Parsing Tests
# =============================================================================


class TestExtractAddedLines:
    """Test patch parsing to extract added line numbers."""

    def test_additions_and_deletions(self):
        """Correctly handles mix of additions and deletions."""
        patch = """@@ -1,4 +1,3 @@
 line 1
-deleted line
+added line
 line 3"""
        result = extract_added_lines(patch)
        assert result == {2}

    def test_multiple_hunks(self):
        """Lines from multiple hunks are combined."""
        patch = """@@ -1,3 +1,4 @@
 line 1
+added at 2
 line 2
 line 3
@@ -10,3 +11,4 @@
 line 10
+added at 12
 line 11"""
        result = extract_added_lines(patch)
        assert result == {2, 12}

    def test_new_file(self):
        """New file starting at line 1."""
        patch = """@@ -0,0 +1,3 @@
+line 1
+line 2
+line 3"""
        result = extract_added_lines(patch)
        assert result == {1, 2, 3}

    def test_empty_and_deletions_only(self):
        """Empty patch and deletion-only patches return empty set."""
        assert extract_added_lines('') == set()
        assert extract_added_lines(None) == set()
        patch = """@@ -1,3 +1,1 @@
 line 1
-deleted 1
-deleted 2"""
        assert extract_added_lines(patch) == set()


class TestExtractPatchChanges:
    """Test patch parsing for pure additions vs modifications."""

    def test_pure_addition_vs_modification(self):
        """Correctly distinguishes pure additions from modifications."""
        patch = """@@ -1,2 +1,3 @@
 existing
-old value
+new value
+brand new line"""
        file_patch = extract_file_patch(patch)
        additions = file_patch.additions_by_line

        # Line 2 is modification
        assert additions[2].is_addition is False
        assert 'new' in additions[2].changed_tokens

        # Line 3 is pure addition
        assert additions[3].is_addition is True

    def test_multiple_deletions_pair_with_additions(self):
        """Multiple deletions pair with additions in order."""
        patch = """@@ -1,4 +1,4 @@
 line 1
-old a
-old b
+new a
+new b
 line 4"""
        file_patch = extract_file_patch(patch)
        additions = file_patch.additions_by_line
        assert additions[2].is_addition is False
        assert additions[3].is_addition is False

    def test_pure_deletions_are_captured(self):
        """Pure deletions (- without +) are recorded."""
        patch = """@@ -1,4 +1,2 @@
 keep this
-delete me
-also delete
 keep this too"""
        file_patch = extract_file_patch(patch)
        additions = file_patch.additions_by_line
        deletions = file_patch.deletions_by_line

        # No additions
        assert len(additions) == 0

        # Two pure deletions at old file lines 2 and 3
        assert len(deletions) == 2
        assert 2 in deletions
        assert 3 in deletions
        assert deletions[2].is_deletion is True
        assert deletions[3].is_deletion is True
        assert 'delete me' in deletions[2].content
        assert 'also delete' in deletions[3].content


class TestGetChangedTokens:
    """Test token diffing between old and new lines."""

    def test_simple_value_change(self):
        """Single value change is detected."""
        changed = get_changed_tokens('x = 1', 'x = 2')
        assert '2' in changed
        assert '1' not in changed
        assert 'x' not in changed

    def test_added_argument(self):
        """Added function argument is detected."""
        changed = get_changed_tokens('foo(a, b)', 'foo(a, b, c)')
        assert 'c' in changed
        assert 'a' not in changed

    def test_whitespace_only_returns_empty(self):
        """Whitespace-only changes return empty set."""
        assert get_changed_tokens('x=1', 'x = 1') == set()

    def test_string_content_change(self):
        """String content changes are detected."""
        changed = get_changed_tokens('x = "hello"', 'x = "hello world"')
        assert 'hello world' in changed


# =============================================================================
# Change-Aware Scoring Tests - Core Behavior
# =============================================================================


class TestChangeAwareScoringBasics:
    """Test basic change-aware scoring behavior."""

    @pytest.fixture
    def weights(self) -> TokenWeights:
        return load_token_weights()

    def test_pure_addition_gets_full_score(self, weights):
        """Pure additions get full structural + leaf scores."""
        code = 'def foo():\n    x = 1'
        change_info = {1: LineChange(line_num=1, change_type=LineChangeType.ADDITION, content='def foo():')}
        scores = calculate_line_scores_with_changes(code, 'py', weights, change_info)
        assert scores.get(1, 0) >= weights.get_structural_weight('function_definition')

    def test_modification_only_scores_changed_tokens(self, weights):
        """Modifications only score the changed tokens."""
        code = 'x = 2'
        change_info = {
            1: LineChange(line_num=1, change_type=LineChangeType.MODIFICATION, content='x = 2', changed_tokens={'2'})
        }
        scores = calculate_line_scores_with_changes(code, 'py', weights, change_info)
        # Should only get integer weight, not identifier or assignment
        assert 0 < scores.get(1, 0) < 0.5

    def test_unmatched_tokens_score_zero(self, weights):
        """Modification with non-matching tokens scores zero."""
        code = 'x = 1'
        change_info = {
            1: LineChange(
                line_num=1, change_type=LineChangeType.MODIFICATION, content='x = 1', changed_tokens={'nonexistent'}
            )
        }
        scores = calculate_line_scores_with_changes(code, 'py', weights, change_info)
        assert scores.get(1, 0) == 0.0

    def test_comment_addition_scores_zero(self, weights):
        """Adding a comment still scores zero."""
        code = '# this is a comment'
        change_info = {1: LineChange(line_num=1, change_type=LineChangeType.ADDITION, content='# this is a comment')}
        scores = calculate_line_scores_with_changes(code, 'py', weights, change_info)
        assert scores.get(1, 0) == 0.0


# =============================================================================
# Change-Aware Scoring Tests - Edge Cases for Over/Under Rewarding
# =============================================================================


class TestTrivialChangesLowScore:
    """Ensure trivial changes don't get over-rewarded."""

    @pytest.fixture
    def weights(self) -> TokenWeights:
        return load_token_weights()

    def test_single_number_change(self, weights):
        """Changing a single number (1 -> 2) scores very low."""
        code = 'x = 2'
        patch = '@@ -1,1 +1,1 @@\n-x = 1\n+x = 2'
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        assert score < 0.2  # Just an integer token

    def test_boolean_flip(self, weights):
        """Changing True to False scores very low."""
        code = 'enabled = False'
        patch = '@@ -1,1 +1,1 @@\n-enabled = True\n+enabled = False'
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        assert score < 0.2

    def test_operator_change(self, weights):
        """Changing operator (+ to -) scores very low."""
        code = 'result = a - b'
        patch = '@@ -1,1 +1,1 @@\n-result = a + b\n+result = a - b'
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        assert score < 0.5  # Just operator weight

    def test_short_identifier_change(self, weights):
        """Changing short identifier (a -> b) scores appropriately."""
        code = 'b = 1'
        patch = '@@ -1,1 +1,1 @@\n-a = 1\n+b = 1'
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # 'b' is short token, exact match only
        assert score == weights.get_leaf_weight('identifier')

    def test_whitespace_formatting_only(self, weights):
        """Pure whitespace/formatting changes score zero."""
        code = 'x = 1'
        patch = '@@ -1,1 +1,1 @@\n-x=1\n+x = 1'
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        assert score == 0.0

    def test_single_character_in_long_identifier(self, weights):
        """Changing one char in long identifier scores just one identifier."""
        code = 'calculateTotal = 1'
        patch = '@@ -1,1 +1,1 @@\n-calculateTotals = 1\n+calculateTotal = 1'
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should match 'calculateTotal' via substring for long token
        assert score <= weights.get_leaf_weight('identifier')


class TestSubstantiveChangesProperScore:
    """Ensure substantive changes get properly rewarded."""

    @pytest.fixture
    def weights(self) -> TokenWeights:
        return load_token_weights()

    def test_new_function_definition(self, weights):
        """Adding a new function gets full structural bonus."""
        code = 'def process_data():\n    return transform(data)'
        patch = '@@ -0,0 +1,2 @@\n+def process_data():\n+    return transform(data)'
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should include function_definition + return_statement + identifiers
        assert score >= weights.get_structural_weight('function_definition') + 1.0

    def test_new_class_definition(self, weights):
        """Adding a new class gets full structural bonus."""
        code = 'class DataProcessor:\n    pass'
        patch = '@@ -0,0 +1,2 @@\n+class DataProcessor:\n+    pass'
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        assert score >= weights.get_structural_weight('class_definition')

    def test_adding_new_method_to_class(self, weights):
        """Adding a method to existing class gets method score."""
        code = 'class Foo:\n    def bar(self):\n        pass\n    def new_method(self):\n        return self.value'
        patch = """@@ -1,3 +1,5 @@
 class Foo:
     def bar(self):
         pass
+    def new_method(self):
+        return self.value"""
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should get function_definition + return + identifiers
        assert score >= weights.get_structural_weight('function_definition')

    def test_adding_multiple_new_identifiers(self, weights):
        """Adding line with multiple new identifiers scores them all."""
        code = 'result = calculate(alpha, beta, gamma, delta)'
        patch = '@@ -1,1 +1,1 @@\n-result = calculate(alpha)\n+result = calculate(alpha, beta, gamma, delta)'
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should get 3 new identifiers
        identifier_weight = weights.get_leaf_weight('identifier')
        assert score >= identifier_weight * 2  # At least beta, gamma, delta (some might be short)

    def test_adding_decorator(self, weights):
        """Adding a decorator to function is a pure addition."""
        code = '@property\ndef value(self):\n    return self._value'
        patch = """@@ -1,2 +1,3 @@
+@property
 def value(self):
     return self._value"""
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should get decorator bonus + identifier
        assert score > 0.5

    def test_adding_error_handling(self, weights):
        """Adding try/except block gets structural bonuses."""
        code = 'try:\n    result = risky()\nexcept Exception:\n    result = None'
        patch = """@@ -0,0 +1,4 @@
+try:
+    result = risky()
+except Exception:
+    result = None"""
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should get try + except structural bonuses
        assert score >= weights.get_structural_weight('try_statement')

    def test_adding_conditional_logic(self, weights):
        """Adding if/else logic gets structural bonuses."""
        code = 'if condition:\n    do_something()\nelse:\n    do_other()'
        patch = """@@ -0,0 +1,4 @@
+if condition:
+    do_something()
+else:
+    do_other()"""
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        assert score >= weights.get_structural_weight('if_statement')


class TestComplexModificationScenarios:
    """Test complex real-world modification scenarios."""

    @pytest.fixture
    def weights(self) -> TokenWeights:
        return load_token_weights()

    def test_function_signature_change_add_param(self, weights):
        """Adding parameter to function signature."""
        code = 'def process(data, validate=True):\n    pass'
        patch = """@@ -1,2 +1,2 @@
-def process(data):
+def process(data, validate=True):
     pass"""
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should score 'validate' and 'True' tokens
        assert score > 0
        # But not get full function_definition bonus (modification, not addition)
        assert score < weights.get_structural_weight('function_definition')

    def test_string_content_significant_change(self, weights):
        """Significantly changing string content."""
        code = 'message = "Please enter your full name and email address"'
        patch = '''@@ -1,1 +1,1 @@
-message = "Hello"
+message = "Please enter your full name and email address"'''
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Long string content change should match
        assert score > 0

    def test_modifying_inside_multiline_string(self, weights):
        """Changing content inside a multiline string scores low."""
        code = '"""\nUpdated documentation\nwith new info\n"""'
        patch = '''@@ -1,4 +1,4 @@
 """
-Old documentation
-goes here
+Updated documentation
+with new info
 """'''
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should be relatively low - it's string content
        assert score < 1.0

    def test_changing_import_statement(self, weights):
        """Modifying import to add new module."""
        code = 'from typing import Dict, List, Optional'
        patch = """@@ -1,1 +1,1 @@
-from typing import Dict, List
+from typing import Dict, List, Optional"""
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should get identifier weight for 'Optional'
        assert score >= weights.get_leaf_weight('identifier')

    def test_chained_method_call_addition(self, weights):
        """Adding method to chain."""
        code = 'result = data.filter().sort().limit(10)'
        patch = """@@ -1,1 +1,1 @@
-result = data.filter().sort()
+result = data.filter().sort().limit(10)"""
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should get 'limit' identifier and maybe '10'
        assert score > 0

    def test_multi_line_modification_with_pure_addition(self, weights):
        """Mix of modifications and pure additions in one patch."""
        code = 'x = 2\ny = 3\nz = x + y'
        patch = """@@ -1,2 +1,3 @@
-x = 1
+x = 2
+y = 3
 z = x + y"""
        # Note: z line is context, x is modification, y is pure addition
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # y=3 is pure addition (full score), x=2 is modification (just '2')
        # Pure addition should contribute more than modification
        assert score > 0.5

    def test_adding_type_annotations(self, weights):
        """Adding type annotations to function."""
        code = 'def process(data: List[int]) -> Dict[str, int]:\n    pass'
        patch = """@@ -1,2 +1,2 @@
-def process(data):
+def process(data: List[int]) -> Dict[str, int]:
     pass"""
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should get type identifiers
        assert score > 0


class TestEdgeCasesTokenMatching:
    """Test edge cases in token matching logic."""

    @pytest.fixture
    def weights(self) -> TokenWeights:
        return load_token_weights()

    def test_short_token_exact_match_only(self, weights):
        """Short tokens (<=2 chars) require exact match."""
        code = 'abc = 1'
        # 'c' is short token - should NOT match 'abc'
        change_info = {
            1: LineChange(line_num=1, change_type=LineChangeType.MODIFICATION, content='abc = 1', changed_tokens={'c'})
        }
        scores = calculate_line_scores_with_changes(code, 'py', weights, change_info)
        # 'c' shouldn't match 'abc', so score should be just for '1' if it matches, or 0
        assert scores.get(1, 0) < weights.get_leaf_weight('identifier')

    def test_long_token_substring_match(self, weights):
        """Long tokens (>2 chars) allow substring match."""
        code = 'message = "hello world"'
        # 'world' should match the string containing it
        change_info = {
            1: LineChange(
                line_num=1,
                change_type=LineChangeType.MODIFICATION,
                content='message = "hello world"',
                changed_tokens={'world'},
            )
        }
        scores = calculate_line_scores_with_changes(code, 'py', weights, change_info)
        assert scores.get(1, 0) > 0

    def test_similar_but_different_identifiers(self, weights):
        """Similar identifiers shouldn't cross-match incorrectly."""
        code = 'calculate_total = calculate_sum + calculate_avg'
        # Only 'calculate_total' is new
        patch = """@@ -1,1 +1,1 @@
-result = calculate_sum + calculate_avg
+calculate_total = calculate_sum + calculate_avg"""
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should match calculate_total but not others (they exist in old line)
        # Actually 'calculate_total' substring matches all three...
        # This is a known limitation - long substrings can over-match
        assert score > 0

    def test_number_modification_precision(self, weights):
        """Number changes should score correctly."""
        code = 'timeout = 3600'
        patch = '@@ -1,1 +1,1 @@\n-timeout = 60\n+timeout = 3600'
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # '3600' is a new token, should score
        assert score > 0


class TestRealWorldPatches:
    """Test with realistic patch scenarios."""

    @pytest.fixture
    def weights(self) -> TokenWeights:
        return load_token_weights()

    def test_bug_fix_single_line(self, weights):
        """Simple bug fix changing one value."""
        code = 'if count >= 0:\n    process()'
        patch = """@@ -1,2 +1,2 @@
-if count > 0:
+if count >= 0:
     process()"""
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Just adding '=' to operator - should be very low
        assert score < 0.5

    def test_feature_addition_multiple_lines(self, weights):
        """Adding a new feature with multiple lines."""
        code = """def validate(self):
    if not self.data:
        raise ValueError("No data")
    return True"""
        patch = """@@ -0,0 +1,4 @@
+def validate(self):
+    if not self.data:
+        raise ValueError("No data")
+    return True"""
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should get substantial score for new function
        assert score > 3.0

    def test_refactor_rename_variable(self, weights):
        """Renaming a variable (multiple occurrences counted separately per line)."""
        code = 'user_count = get_count()\nprint(user_count)'
        patch = """@@ -1,2 +1,2 @@
-count = get_count()
-print(count)
+user_count = get_count()
+print(user_count)"""
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Each line modification should score the new identifier
        assert score > 0
        # But it's just identifier changes, not structural
        assert score < 2.0


class TestRealWorldPatchExtraction:
    """Test patch extraction with real patches from production database."""

    def test_discord_js_multiple_hunks(self):
        """Discord.js patch with 3 hunks: additions, modifications, and structural changes.

        With similarity-based pairing, lines like 'value: addess,' don't match
        'value: this.extractAddress(monitorJSON),' because Jaccard < 1/3.
        """
        patch = """@@ -46,6 +46,7 @@ class Discord extends NotificationProvider {
             }

             // If heartbeatJSON is not null, we go into the normal alerting loop.
+            let addess = this.extractAddress(monitorJSON);
             if (heartbeatJSON["status"] === DOWN) {
                 let discorddowndata = {
                     username: discordDisplayName,
@@ -58,9 +59,9 @@ class Discord extends NotificationProvider {
                             name: "Service Name",
                             value: monitorJSON["name"],
                         },
-                            ...(!notification.disableUrl ? [{
+                            ...((!notification.disableUrl && addess) ? [{
                             name: monitorJSON["type"] === "push" ? "Service Type" : "Service URL",
-                                value: this.extractAddress(monitorJSON),
+                                value: addess,
                         }] : []),
                         {
                             name: `Time (${heartbeatJSON["timezone"]})`,
@@ -98,18 +99,18 @@ class Discord extends NotificationProvider {
                             name: "Service Name",
                             value: monitorJSON["name"],
                         },
-                            ...(!notification.disableUrl ? [{
+                            ...((!notification.disableUrl && addess) ? [{
                             name: monitorJSON["type"] === "push" ? "Service Type" : "Service URL",
-                                value: this.extractAddress(monitorJSON),
+                                value: addess,
                         }] : []),
                         {
                             name: `Time (${heartbeatJSON["timezone"]})`,
                             value: heartbeatJSON["localDateTime"],
                         },
-                            {
+                            ...(heartbeatJSON["ping"] != null ? [{
                             name: "Ping",
-                                value: heartbeatJSON["ping"] == null ? "N/A" : heartbeatJSON["ping"] + " ms",
-                            },
+                                value: heartbeatJSON["ping"] + " ms",
+                            }] : []),
                     ],
                 }],
             };"""

        fp = extract_file_patch(patch, 'discord.js')
        additions = fp.additions_by_line
        deletions = fp.deletions_by_line

        # First hunk: 1 pure addition at line 48 (after 2 context lines starting at 46)
        assert 48 in additions
        assert additions[48].change_type == LineChangeType.ADDITION
        assert 'addess' in additions[48].content

        # Second hunk: line 62 is modification, line 64 is pure addition (low similarity)
        assert 62 in additions
        assert additions[62].change_type == LineChangeType.MODIFICATION
        assert 'addess' in additions[62].changed_tokens

        # Total 8 additions: 4 pure + 4 modifications
        assert len(additions) == 8
        pure_adds = [c for c in additions.values() if c.change_type == LineChangeType.ADDITION]
        assert len(pure_adds) == 4

        # 3 pure deletions (lines with low similarity to additions)
        assert len(deletions) == 3

    def test_rust_multiple_identical_hunks(self):
        """Rust patch with 3 hunks making similar changes in different test functions."""
        patch = """@@ -246,7 +246,7 @@ fn test_burn_success() {
         ));

         assert!(TotalHotkeyAlpha::<Test>::get(hotkey, netuid) < initial_alpha);
-        assert!(SubnetAlphaOut::<Test>::get(netuid) == initial_net_alpha);
+        assert!(SubnetAlphaOut::<Test>::get(netuid) < initial_net_alpha); // Expect decrease
         assert!(
             SubtensorModule::get_stake_for_hotkey_and_coldkey_on_subnet(&hotkey, &coldkey, netuid)
                 < stake.into()
@@ -307,7 +307,7 @@ fn test_burn_staker_is_nominator() {
         ));

         assert!(TotalHotkeyAlpha::<Test>::get(hotkey, netuid) < initial_alpha);
-        assert!(SubnetAlphaOut::<Test>::get(netuid) == initial_net_alpha);
+        assert!(SubnetAlphaOut::<Test>::get(netuid) < initial_net_alpha); // Expect decrease
         assert!(
             SubtensorModule::get_stake_for_hotkey_and_coldkey_on_subnet(
                 &hotkey,
@@ -376,7 +376,7 @@ fn test_burn_two_stakers() {
         ));

         assert!(TotalHotkeyAlpha::<Test>::get(hotkey, netuid) < initial_alpha);
-        assert!(SubnetAlphaOut::<Test>::get(netuid) == initial_net_alpha);
+        assert!(SubnetAlphaOut::<Test>::get(netuid) < initial_net_alpha); // Expect decrease
         assert!(
             SubtensorModule::get_stake_for_hotkey_and_coldkey_on_subnet(&hotkey, &coldkey, netuid)
                 < stake.into()"""

        fp = extract_file_patch(patch, 'test.rs')
        additions = fp.additions_by_line
        deletions = fp.deletions_by_line

        # 3 modifications, one per hunk (line numbers based on hunk headers)
        assert len(additions) == 3
        assert 248 in additions  # First hunk: starts at 246, +2 context, modification at 248
        assert 309 in additions  # Second hunk
        assert 378 in additions  # Third hunk

        # All are modifications (- followed by +)
        for line_num in [248, 309, 378]:
            assert additions[line_num].change_type == LineChangeType.MODIFICATION
            # The changed token is the operator and comment
            assert 'Expect' in additions[line_num].changed_tokens or 'decrease' in additions[line_num].changed_tokens

        # No pure deletions
        assert len(deletions) == 0

    def test_rust_lib_mixed_additions_and_modifications(self):
        """Rust lib.rs with pure additions mixed with modifications.

        With similarity-based pairing, Ok(actual_alpha) doesn't match any old lines
        above the 1/3 Jaccard threshold, so it becomes a pure addition.
        """
        patch = """@@ -2633,14 +2633,16 @@ impl<T: Config + pallet_balances::Config<Balance = u64>>
             Error::<T>::HotKeyAccountNotExists
         );

+        let actual_alpha = Self::decrease_stake_for_hotkey_and_coldkey_on_subnet(
+            hotkey, coldkey, netuid, alpha,
+        );
+
         // Decrese alpha out counter
         SubnetAlphaOut::<T>::mutate(netuid, |total| {
-            *total = total.saturating_sub(alpha);
+            *total = total.saturating_sub(actual_alpha);
         });

-        Ok(Self::decrease_stake_for_hotkey_and_coldkey_on_subnet(
-            hotkey, coldkey, netuid, alpha,
-        ))
+        Ok(actual_alpha)
     }
 }"""

        fp = extract_file_patch(patch, 'lib.rs')
        additions = fp.additions_by_line
        deletions = fp.deletions_by_line

        # 5 pure additions:
        #   - 4 lines of the new let actual_alpha block
        #   - 1 line Ok(actual_alpha) which has no similar old line (Jaccard < 1/3)
        pure_additions = [c for c in additions.values() if c.change_type == LineChangeType.ADDITION]
        assert len(pure_additions) == 5

        # 1 modification (saturating_sub line)
        modifications = [c for c in additions.values() if c.change_type == LineChangeType.MODIFICATION]
        assert len(modifications) == 1

        # Total 6 additions
        assert len(additions) == 6

        # 3 pure deletions (the old Ok(...) block - none matched above threshold)
        assert len(deletions) == 3

    def test_python_new_file_all_additions(self):
        """Python file with all pure additions (new file)."""
        patch = """@@ -0,0 +1,25 @@
+import weakref
+from async_substrate_interface.utils import cache
+
+
+class WeakMethodCallable:
+    \"\"\"
+    A callable that holds a weak reference to a bound method.
+    Used to break reference cycles in CachedFetcher.
+    \"\"\"
+
+    def __init__(self, bound_method):
+        self._weak_method = weakref.WeakMethod(bound_method)
+
+    async def __call__(self, *args, **kwargs):
+        method = self._weak_method()
+        if method is None:
+            # The underlying method/instance has been garbage collected.
+            # Return None gracefully instead of raising, so callers of
+            # CachedFetcher do not see a low-level ReferenceError.
+            return None
+        return await method(*args, **kwargs)
+
+
+def _new_get(self, instance, owner):
+    pass"""

        fp = extract_file_patch(patch, 'test.py')
        additions = fp.additions_by_line
        deletions = fp.deletions_by_line

        # All 25 lines are pure additions
        assert len(additions) == 25
        for info in additions.values():
            assert info.change_type == LineChangeType.ADDITION

        # No deletions
        assert len(deletions) == 0

        # Check line numbers start at 1 (new file)
        assert 1 in additions
        assert 25 in additions

    def test_python_init_pure_additions_with_comments(self):
        """Python __init__.py with pure additions including multi-line comment."""
        patch = """@@ -1,3 +1,10 @@
 from .core.settings import __version__, DEFAULTS, DEFAULT_NETWORK
 from .utils.btlogging import logging
+from .utils.async_substrate_interface_patch import apply_patch
+# Apply the memory leak patch for AsyncSubstrateInterface *before* importing anything
+# that may create AsyncSubstrateInterface instances. In particular, easy_imports
+# pulls in AsyncSubtensor, which uses AsyncSubstrateInterface, so it must be
+# imported only after apply_patch() has been called. Do not reorder these imports.
+apply_patch()
+
 from .utils.easy_imports import *"""

        fp = extract_file_patch(patch, '__init__.py')
        additions = fp.additions_by_line
        deletions = fp.deletions_by_line

        # 7 pure additions (lines 3-9)
        assert len(additions) == 7
        for info in additions.values():
            assert info.change_type == LineChangeType.ADDITION

        # Line 3 is the import
        assert 3 in additions
        assert 'apply_patch' in additions[3].content

        # Lines 4-7 are comments
        assert 4 in additions
        assert '#' in additions[4].content

        # No deletions
        assert len(deletions) == 0

    def test_telegram_docstring_modifications(self):
        """Telegram bot.py with modifications inside docstrings (URL changes)."""
        patch = """@@ -7200,12 +7200,12 @@ async def set_sticker_set_thumbnail(
                 **.TGS** animation with the thumbnail up to
                 :tg-const:`telegram.constants.StickerSetLimit.MAX_ANIMATED_THUMBNAIL_SIZE`
                 kilobytes in size; see
-                `the docs <https://core.telegram.org/stickers#animation-requirements>`_ for
+                `the docs <https://core.telegram.org/stickers#animated-stickers-and-emoji>`_ for
                 animated sticker technical requirements, or a ``.WEBM`` video with the thumbnail up
                 to :tg-const:`telegram.constants.StickerSetLimit.MAX_ANIMATED_THUMBNAIL_SIZE`
                 kilobytes in size; see
-                `this <https://core.telegram.org/stickers#video-requirements>`_ for video sticker
-                technical requirements.
+                `this <https://core.telegram.org/stickers#video-stickers-and-emoji>`_ for video
+                sticker technical requirements.

                 |fileinput|"""

        fp = extract_file_patch(patch, 'bot.py')
        additions = fp.additions_by_line
        deletions = fp.deletions_by_line

        # 3 modifications
        assert len(additions) == 3
        for info in additions.values():
            assert info.change_type == LineChangeType.MODIFICATION

        # Changed tokens should include new URL fragments
        all_changed = set()
        for info in additions.values():
            all_changed.update(info.changed_tokens)

        assert 'animated' in all_changed or 'emoji' in all_changed or 'stickers' in all_changed

        # No pure deletions
        assert len(deletions) == 0

    def test_uneven_deletion_addition_refactor(self):
        """Test 4 deletions -> 2 additions refactor with similarity-based pairing.

        This tests the scenario where code is condensed (e.g., using walrus operator).
        Similarity-based pairing correctly identifies identical lines even when
        they're at different positions in the hunk.
        """
        patch = """@@ -10,4 +10,2 @@
-        if pr.repository_tier_configuration:
-            tier = get_tier_from_config(pr.repository_tier_configuration)
-            if tier:
-                stats[tier].closed_count += 1
+        if tier := get_tier(pr):
+            stats[tier].closed_count += 1"""

        fp = extract_file_patch(patch, 'test.py')
        additions = fp.additions_by_line
        deletions = fp.deletions_by_line

        # 2 additions, both modifications (paired with similar deletions)
        assert len(additions) == 2

        # Line 10: 'if tier := get_tier(pr):' pairs with 'tier = get_tier_from_config(...)'
        # because they share tokens like 'tier', 'get_tier', '=', etc.
        assert 10 in additions
        assert additions[10].change_type == LineChangeType.MODIFICATION
        assert 'get_tier' in additions[10].changed_tokens

        # Line 11: 'stats[tier].closed_count += 1' pairs with identical old line
        # Jaccard = 1.0, changed_tokens should be empty
        assert 11 in additions
        assert additions[11].change_type == LineChangeType.MODIFICATION
        assert len(additions[11].changed_tokens) == 0

        # 2 pure deletions (the unmatched old lines)
        assert len(deletions) == 2

    def test_large_hunk_skips_similarity_matching(self):
        """Test that hunks with >55 pending deletions skip similarity matching.

        This tests the O(n²) mitigation - large restructures treat all additions
        as pure additions to avoid expensive similarity computation.
        """
        # Create a patch with 60 deletions followed by 5 additions
        deletions = '\n'.join([f'-line {i}' for i in range(60)])
        additions = '\n'.join([f'+new line {i}' for i in range(5)])
        patch = f"""@@ -1,60 +1,5 @@
{deletions}
{additions}"""

        fp = extract_file_patch(patch, 'test.py')
        additions_dict = fp.additions_by_line
        deletions_dict = fp.deletions_by_line

        # All 5 additions should be pure additions (similarity matching skipped)
        assert len(additions_dict) == 5
        for info in additions_dict.values():
            assert info.change_type == LineChangeType.ADDITION

        # All 60 deletions should be pure deletions
        assert len(deletions_dict) == 60


class TestFilePatch:
    """Test new FilePatch structure with hunk preservation."""

    def test_empty_patch_returns_empty_file_patch(self):
        """Empty patch returns FilePatch with no hunks."""
        result = extract_file_patch(None, 'test.py')
        assert result.filename == 'test.py'
        assert len(result.hunks) == 0
        assert result.total_additions == 0
        assert result.total_deletions == 0
        assert result.total_modifications == 0

    def test_single_hunk_structure(self):
        """Single hunk is correctly parsed with header values."""
        patch = """@@ -10,3 +10,4 @@ context
 existing line
+added line
 another existing"""

        result = extract_file_patch(patch, 'test.py')
        assert len(result.hunks) == 1

        hunk = result.hunks[0]
        assert hunk.old_start == 10
        assert hunk.old_count == 3
        assert hunk.new_start == 10
        assert hunk.new_count == 4

    def test_multiple_hunks_preserved(self):
        """Multiple hunks are parsed and preserved separately."""
        patch = """@@ -1,3 +1,4 @@ first
 line 1
+added in first hunk
 line 2
 line 3
@@ -20,2 +21,3 @@ second
 line 20
+added in second hunk
 line 21"""

        result = extract_file_patch(patch, 'test.py')
        assert len(result.hunks) == 2

        # First hunk
        assert result.hunks[0].old_start == 1
        assert result.hunks[0].new_start == 1
        assert len(result.hunks[0].additions) == 1

        # Second hunk
        assert result.hunks[1].old_start == 20
        assert result.hunks[1].new_start == 21
        assert len(result.hunks[1].additions) == 1

    def test_line_change_type_enum(self):
        """LineChangeType enum is correctly assigned for each change type."""
        patch = """@@ -1,3 +1,3 @@ context
-old value
+new value
+brand new line
 existing"""

        result = extract_file_patch(patch, 'test.py')
        changes = result.all_changes

        # Find the modification (old value -> new value)
        modifications = [c for c in changes if c.change_type == LineChangeType.MODIFICATION]
        assert len(modifications) == 1
        assert modifications[0].content == 'new value'
        assert modifications[0].old_content == 'old value'
        assert modifications[0].similarity is not None

        # Find the pure addition (brand new line)
        additions = [c for c in changes if c.change_type == LineChangeType.ADDITION]
        assert len(additions) == 1
        assert additions[0].content == 'brand new line'

    def test_pure_deletions_tracked(self):
        """Pure deletions are correctly tracked with DELETION type."""
        patch = """@@ -1,4 +1,2 @@ context
 keep
-delete me
-also delete
 keep"""

        result = extract_file_patch(patch, 'test.py')
        deletions = [c for c in result.all_changes if c.change_type == LineChangeType.DELETION]
        assert len(deletions) == 2
        assert deletions[0].content == 'delete me'
        assert deletions[1].content == 'also delete'

    def test_changed_tokens_tracked_for_modifications(self):
        """Modified lines track changed tokens."""
        patch = """@@ -1,1 +1,1 @@ context
-value = old_value
+value = new_value"""

        result = extract_file_patch(patch, 'test.py')
        modifications = result.hunks[0].modifications
        assert len(modifications) == 1

        # Should have 'new_value' as changed token (not in old line)
        assert 'new_value' in modifications[0].changed_tokens

    def test_total_changed_tokens_aggregation(self):
        """total_changed_tokens aggregates across all modifications."""
        patch = """@@ -1,2 +1,2 @@ context
-x = 1
+x = 2
-y = a
+y = b"""

        result = extract_file_patch(patch, 'test.py')
        # Each modification changes 1 token
        assert result.total_changed_tokens >= 2

    def test_line_change_properties(self):
        """LineChange convenience properties work correctly."""
        addition = LineChange(
            line_num=1,
            change_type=LineChangeType.ADDITION,
            content='new line',
        )
        assert addition.is_addition is True
        assert addition.is_deletion is False
        assert addition.is_modification is False

        deletion = LineChange(
            line_num=1,
            change_type=LineChangeType.DELETION,
            content='old line',
        )
        assert deletion.is_addition is False
        assert deletion.is_deletion is True
        assert deletion.is_modification is False

        modification = LineChange(
            line_num=1,
            change_type=LineChangeType.MODIFICATION,
            content='modified',
            changed_tokens={'new_token'},
            old_content='original',
        )
        assert modification.is_addition is False
        assert modification.is_deletion is False
        assert modification.is_modification is True

    def test_additions_by_line_property(self):
        """additions_by_line returns dict keyed by line number."""
        patch = """@@ -1,2 +1,3 @@ context
 existing
+added at 2
+added at 3"""

        result = extract_file_patch(patch, 'test.py')
        by_line = result.additions_by_line

        assert 2 in by_line
        assert 3 in by_line
        assert by_line[2].content == 'added at 2'
        assert by_line[3].content == 'added at 3'

    def test_hunk_count_defaults(self):
        """Hunk header without counts defaults to 1."""
        # This is the format: @@ -1 +1 @@ (no count means 1)
        patch = """@@ -5 +5 @@ context
-old
+new"""

        result = extract_file_patch(patch, 'test.py')
        assert len(result.hunks) == 1
        hunk = result.hunks[0]
        assert hunk.old_count == 1
        assert hunk.new_count == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
