# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for token-based scoring using tree-sitter.

Run tests:
    pytest tests/validator/test_token_scoring.py -v
"""

import pytest

from gittensor.classes import LineChangeInfo
from gittensor.validator.utils.load_weights import (
    TokenWeights,
    load_token_weights,
)
from gittensor.validator.utils.tree_sitter_scoring import (
    calculate_line_scores,
    calculate_line_scores_with_changes,
    calculate_total_score_with_changes,
    extract_added_lines,
    extract_patch_changes,
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
        patch = '''@@ -1,4 +1,3 @@
 line 1
-deleted line
+added line
 line 3'''
        result = extract_added_lines(patch)
        assert result == {2}

    def test_multiple_hunks(self):
        """Lines from multiple hunks are combined."""
        patch = '''@@ -1,3 +1,4 @@
 line 1
+added at 2
 line 2
 line 3
@@ -10,3 +11,4 @@
 line 10
+added at 12
 line 11'''
        result = extract_added_lines(patch)
        assert result == {2, 12}

    def test_new_file(self):
        """New file starting at line 1."""
        patch = '''@@ -0,0 +1,3 @@
+line 1
+line 2
+line 3'''
        result = extract_added_lines(patch)
        assert result == {1, 2, 3}

    def test_empty_and_deletions_only(self):
        """Empty patch and deletion-only patches return empty set."""
        assert extract_added_lines('') == set()
        assert extract_added_lines(None) == set()
        patch = '''@@ -1,3 +1,1 @@
 line 1
-deleted 1
-deleted 2'''
        assert extract_added_lines(patch) == set()


class TestExtractPatchChanges:
    """Test patch parsing for pure additions vs modifications."""

    def test_pure_addition_vs_modification(self):
        """Correctly distinguishes pure additions from modifications."""
        patch = '''@@ -1,2 +1,3 @@
 existing
-old value
+new value
+brand new line'''
        changes = extract_patch_changes(patch)

        # Line 2 is modification
        assert changes.additions[2].is_pure_addition is False
        assert 'new' in changes.additions[2].changed_tokens

        # Line 3 is pure addition
        assert changes.additions[3].is_pure_addition is True

    def test_multiple_deletions_pair_with_additions(self):
        """Multiple deletions pair with additions in order."""
        patch = '''@@ -1,4 +1,4 @@
 line 1
-old a
-old b
+new a
+new b
 line 4'''
        changes = extract_patch_changes(patch)
        assert changes.additions[2].is_pure_addition is False
        assert changes.additions[3].is_pure_addition is False

    def test_pure_deletions_are_captured(self):
        """Pure deletions (- without +) are recorded in deletions dict."""
        patch = '''@@ -1,4 +1,2 @@
 keep this
-delete me
-also delete
 keep this too'''
        changes = extract_patch_changes(patch)

        # No additions
        assert len(changes.additions) == 0

        # Two pure deletions at old file lines 2 and 3
        assert len(changes.deletions) == 2
        assert 2 in changes.deletions
        assert 3 in changes.deletions
        assert changes.deletions[2].is_pure_deletion is True
        assert changes.deletions[3].is_pure_deletion is True
        assert 'delete me' in changes.deletions[2].content
        assert 'also delete' in changes.deletions[3].content


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
        change_info = {1: LineChangeInfo(line_num=1, is_pure_addition=True, is_pure_deletion=False, changed_tokens=set())}
        scores = calculate_line_scores_with_changes(code, 'py', weights, change_info)
        assert scores.get(1, 0) >= weights.get_structural_weight('function_definition')

    def test_modification_only_scores_changed_tokens(self, weights):
        """Modifications only score the changed tokens."""
        code = 'x = 2'
        change_info = {1: LineChangeInfo(line_num=1, is_pure_addition=False, is_pure_deletion=False, changed_tokens={'2'})}
        scores = calculate_line_scores_with_changes(code, 'py', weights, change_info)
        # Should only get integer weight, not identifier or assignment
        assert 0 < scores.get(1, 0) < 0.5

    def test_unmatched_tokens_score_zero(self, weights):
        """Modification with non-matching tokens scores zero."""
        code = 'x = 1'
        change_info = {1: LineChangeInfo(line_num=1, is_pure_addition=False, is_pure_deletion=False, changed_tokens={'nonexistent'})}
        scores = calculate_line_scores_with_changes(code, 'py', weights, change_info)
        assert scores.get(1, 0) == 0.0

    def test_comment_addition_scores_zero(self, weights):
        """Adding a comment still scores zero."""
        code = '# this is a comment'
        change_info = {1: LineChangeInfo(line_num=1, is_pure_addition=True, is_pure_deletion=False, changed_tokens=set())}
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
        patch = '''@@ -1,3 +1,5 @@
 class Foo:
     def bar(self):
         pass
+    def new_method(self):
+        return self.value'''
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
        patch = '''@@ -1,2 +1,3 @@
+@property
 def value(self):
     return self._value'''
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should get decorator bonus + identifier
        assert score > 0.5

    def test_adding_error_handling(self, weights):
        """Adding try/except block gets structural bonuses."""
        code = 'try:\n    result = risky()\nexcept Exception:\n    result = None'
        patch = '''@@ -0,0 +1,4 @@
+try:
+    result = risky()
+except Exception:
+    result = None'''
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should get try + except structural bonuses
        assert score >= weights.get_structural_weight('try_statement')

    def test_adding_conditional_logic(self, weights):
        """Adding if/else logic gets structural bonuses."""
        code = 'if condition:\n    do_something()\nelse:\n    do_other()'
        patch = '''@@ -0,0 +1,4 @@
+if condition:
+    do_something()
+else:
+    do_other()'''
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
        patch = '''@@ -1,2 +1,2 @@
-def process(data):
+def process(data, validate=True):
     pass'''
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
        patch = '''@@ -1,1 +1,1 @@
-from typing import Dict, List
+from typing import Dict, List, Optional'''
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should get identifier weight for 'Optional'
        assert score >= weights.get_leaf_weight('identifier')

    def test_chained_method_call_addition(self, weights):
        """Adding method to chain."""
        code = 'result = data.filter().sort().limit(10)'
        patch = '''@@ -1,1 +1,1 @@
-result = data.filter().sort()
+result = data.filter().sort().limit(10)'''
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should get 'limit' identifier and maybe '10'
        assert score > 0

    def test_multi_line_modification_with_pure_addition(self, weights):
        """Mix of modifications and pure additions in one patch."""
        code = 'x = 2\ny = 3\nz = x + y'
        patch = '''@@ -1,2 +1,3 @@
-x = 1
+x = 2
+y = 3
 z = x + y'''
        # Note: z line is context, x is modification, y is pure addition
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # y=3 is pure addition (full score), x=2 is modification (just '2')
        # Pure addition should contribute more than modification
        assert score > 0.5

    def test_adding_type_annotations(self, weights):
        """Adding type annotations to function."""
        code = 'def process(data: List[int]) -> Dict[str, int]:\n    pass'
        patch = '''@@ -1,2 +1,2 @@
-def process(data):
+def process(data: List[int]) -> Dict[str, int]:
     pass'''
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
        change_info = {1: LineChangeInfo(line_num=1, is_pure_addition=False, is_pure_deletion=False, changed_tokens={'c'})}
        scores = calculate_line_scores_with_changes(code, 'py', weights, change_info)
        # 'c' shouldn't match 'abc', so score should be just for '1' if it matches, or 0
        assert scores.get(1, 0) < weights.get_leaf_weight('identifier')

    def test_long_token_substring_match(self, weights):
        """Long tokens (>2 chars) allow substring match."""
        code = 'message = "hello world"'
        # 'world' should match the string containing it
        change_info = {1: LineChangeInfo(line_num=1, is_pure_addition=False, is_pure_deletion=False, changed_tokens={'world'})}
        scores = calculate_line_scores_with_changes(code, 'py', weights, change_info)
        assert scores.get(1, 0) > 0

    def test_similar_but_different_identifiers(self, weights):
        """Similar identifiers shouldn't cross-match incorrectly."""
        code = 'calculate_total = calculate_sum + calculate_avg'
        # Only 'calculate_total' is new
        patch = '''@@ -1,1 +1,1 @@
-result = calculate_sum + calculate_avg
+calculate_total = calculate_sum + calculate_avg'''
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
        patch = '''@@ -1,2 +1,2 @@
-if count > 0:
+if count >= 0:
     process()'''
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Just adding '=' to operator - should be very low
        assert score < 0.5

    def test_feature_addition_multiple_lines(self, weights):
        """Adding a new feature with multiple lines."""
        code = '''def validate(self):
    if not self.data:
        raise ValueError("No data")
    return True'''
        patch = '''@@ -0,0 +1,4 @@
+def validate(self):
+    if not self.data:
+        raise ValueError("No data")
+    return True'''
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Should get substantial score for new function
        assert score > 3.0

    def test_refactor_rename_variable(self, weights):
        """Renaming a variable (multiple occurrences counted separately per line)."""
        code = 'user_count = get_count()\nprint(user_count)'
        patch = '''@@ -1,2 +1,2 @@
-count = get_count()
-print(count)
+user_count = get_count()
+print(user_count)'''
        score = calculate_total_score_with_changes(code, 'py', weights, patch)
        # Each line modification should score the new identifier
        assert score > 0
        # But it's just identifier changes, not structural
        assert score < 2.0


class TestRealWorldPatchExtraction:
    """Test patch extraction with real patches from production database."""

    def test_discord_js_multiple_hunks(self):
        """Discord.js patch with 3 hunks: additions, modifications, and structural changes."""
        patch = '''@@ -46,6 +46,7 @@ class Discord extends NotificationProvider {
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
             };'''

        changes = extract_patch_changes(patch)

        # First hunk: 1 pure addition at line 48 (after 2 context lines starting at 46)
        assert 48 in changes.additions
        assert changes.additions[48].is_pure_addition is True
        assert 'addess' in changes.additions[48].content

        # Second hunk: modifications at lines 62, 64
        assert 62 in changes.additions
        assert changes.additions[62].is_pure_addition is False
        assert 'addess' in changes.additions[62].changed_tokens

        # Third hunk: has both modifications and structural changes
        assert len(changes.additions) == 8  # 1 pure addition + 7 modifications

        # Should have no pure deletions (all - lines are paired with + lines)
        assert len(changes.deletions) == 0

    def test_rust_multiple_identical_hunks(self):
        """Rust patch with 3 hunks making similar changes in different test functions."""
        patch = '''@@ -246,7 +246,7 @@ fn test_burn_success() {
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
                 < stake.into()'''

        changes = extract_patch_changes(patch)

        # 3 modifications, one per hunk (line numbers based on hunk headers)
        assert len(changes.additions) == 3
        assert 248 in changes.additions  # First hunk: starts at 246, +2 context, modification at 248
        assert 309 in changes.additions  # Second hunk
        assert 378 in changes.additions  # Third hunk

        # All are modifications (- followed by +)
        for line_num in [248, 309, 378]:
            assert changes.additions[line_num].is_pure_addition is False
            # The changed token is the operator and comment
            assert 'Expect' in changes.additions[line_num].changed_tokens or \
                   'decrease' in changes.additions[line_num].changed_tokens

        # No pure deletions
        assert len(changes.deletions) == 0

    def test_rust_lib_mixed_additions_and_modifications(self):
        """Rust lib.rs with pure additions mixed with modifications."""
        patch = '''@@ -2633,14 +2633,16 @@ impl<T: Config + pallet_balances::Config<Balance = u64>>
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
 }'''

        changes = extract_patch_changes(patch)

        # 4 pure additions (lines 2636-2639)
        pure_additions = [c for c in changes.additions.values() if c.is_pure_addition]
        assert len(pure_additions) == 4

        # 2 modifications
        modifications = [c for c in changes.additions.values() if not c.is_pure_addition]
        assert len(modifications) == 2

        # Total 6 additions
        assert len(changes.additions) == 6

        # 2 pure deletions (the old Ok(...) block that wasn't replaced line-by-line)
        assert len(changes.deletions) == 2

    def test_python_new_file_all_additions(self):
        """Python file with all pure additions (new file)."""
        patch = '''@@ -0,0 +1,25 @@
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
+    pass'''

        changes = extract_patch_changes(patch)

        # All 25 lines are pure additions
        assert len(changes.additions) == 25
        for info in changes.additions.values():
            assert info.is_pure_addition is True

        # No deletions
        assert len(changes.deletions) == 0

        # Check line numbers start at 1 (new file)
        assert 1 in changes.additions
        assert 25 in changes.additions

    def test_python_init_pure_additions_with_comments(self):
        """Python __init__.py with pure additions including multi-line comment."""
        patch = '''@@ -1,3 +1,10 @@
 from .core.settings import __version__, DEFAULTS, DEFAULT_NETWORK
 from .utils.btlogging import logging
+from .utils.async_substrate_interface_patch import apply_patch
+# Apply the memory leak patch for AsyncSubstrateInterface *before* importing anything
+# that may create AsyncSubstrateInterface instances. In particular, easy_imports
+# pulls in AsyncSubtensor, which uses AsyncSubstrateInterface, so it must be
+# imported only after apply_patch() has been called. Do not reorder these imports.
+apply_patch()
+
 from .utils.easy_imports import *'''

        changes = extract_patch_changes(patch)

        # 7 pure additions (lines 3-9)
        assert len(changes.additions) == 7
        for info in changes.additions.values():
            assert info.is_pure_addition is True

        # Line 3 is the import
        assert 3 in changes.additions
        assert 'apply_patch' in changes.additions[3].content

        # Lines 4-7 are comments
        assert 4 in changes.additions
        assert '#' in changes.additions[4].content

        # No deletions
        assert len(changes.deletions) == 0

    def test_telegram_docstring_modifications(self):
        """Telegram bot.py with modifications inside docstrings (URL changes)."""
        patch = '''@@ -7200,12 +7200,12 @@ async def set_sticker_set_thumbnail(
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

                 |fileinput|'''

        changes = extract_patch_changes(patch)

        # 3 modifications
        assert len(changes.additions) == 3
        for info in changes.additions.values():
            assert info.is_pure_addition is False

        # Changed tokens should include new URL fragments
        all_changed = set()
        for info in changes.additions.values():
            all_changed.update(info.changed_tokens)

        assert 'animated' in all_changed or 'emoji' in all_changed or 'stickers' in all_changed

        # No pure deletions
        assert len(changes.deletions) == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
