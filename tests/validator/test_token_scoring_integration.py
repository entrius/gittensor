"""
Integration tests for token scoring pipeline.

These tests exercise the full scoring pipeline with real patches and file contents
from production, verifying:
- Token change matching between old and new lines
- Structural vs leaf score breakdown
- Comment exclusion from scoring
- Lines with score tracking

Run tests:
    pytest tests/validator/test_token_scoring_integration.py -v
"""

import pytest

from gittensor.validator.utils.load_weights import TokenWeights, load_token_weights
from gittensor.validator.utils.tree_sitter_scoring import (
    calculate_score_with_breakdown,
    extract_patch_changes,
)


class TestFullScoringPipeline:
    """Integration tests with real patches and file contents."""

    @pytest.fixture
    def weights(self) -> TokenWeights:
        return load_token_weights()

    def test_rust_lib_mixed_changes_full_scoring(self, weights):
        """
        Test scoring of Rust lib.rs with mixed additions and modifications.

        This patch:
        - Adds 4 new lines (variable declaration)
        - Modifies 2 existing lines (using new variable)
        - Has 2 pure deletions (old return statement)

        Note: Line numbers in patch adjusted to match file content for testing.
        """
        # Patch with line numbers matching our test file content
        patch = """@@ -1,14 +1,16 @@ impl<T: Config>
 impl<T: Config + pallet_balances::Config<Balance = u64>> SomeTrait for Module<T> {
     fn burn_alpha(hotkey: &T::AccountId, coldkey: &T::AccountId, netuid: u16, alpha: u64) -> Result<u64, Error> {
         ensure!(
             Self::hotkey_account_exists(hotkey),
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

        # File content (the NEW version after the patch)
        file_content = """impl<T: Config + pallet_balances::Config<Balance = u64>> SomeTrait for Module<T> {
    fn burn_alpha(hotkey: &T::AccountId, coldkey: &T::AccountId, netuid: u16, alpha: u64) -> Result<u64, Error> {
        ensure!(
            Self::hotkey_account_exists(hotkey),
            Error::<T>::HotKeyAccountNotExists
        );

        let actual_alpha = Self::decrease_stake_for_hotkey_and_coldkey_on_subnet(
            hotkey, coldkey, netuid, alpha,
        );

        // Decrese alpha out counter
        SubnetAlphaOut::<T>::mutate(netuid, |total| {
            *total = total.saturating_sub(actual_alpha);
        });

        Ok(actual_alpha)
    }
}"""

        # Run scoring
        breakdown = calculate_score_with_breakdown(file_content, 'rs', weights, patch)

        # Verify patch parsing
        patch_changes = extract_patch_changes(patch)
        assert len(patch_changes.additions) == 6  # 4 pure additions + 2 modifications
        assert len(patch_changes.deletions) == 2  # 2 pure deletions

        # Verify scoring results
        assert breakdown.total_score > 0, 'Should have positive score'
        assert breakdown.lines_with_score > 0, 'Should have lines that scored'

        # Pure additions should get structural bonuses (let binding)
        assert breakdown.structural_count > 0, 'Should have structural elements (let binding)'
        assert breakdown.structural_score > 0, 'Should have structural score'

        # Should have leaf tokens scored
        assert breakdown.leaf_count > 0, 'Should have leaf tokens'
        assert breakdown.leaf_score > 0, 'Should have leaf score'

        # The comment line should NOT contribute to lines_with_score
        # (line with "// Decrese alpha out counter" is context, not added)
        # Lines with score should be <= total additions
        assert breakdown.lines_with_score <= len(patch_changes.additions)

        # Verify score breakdown adds up
        assert abs(breakdown.total_score - (breakdown.structural_score + breakdown.leaf_score)) < 0.01

        # Print breakdown for debugging
        print('\nRust mixed changes scoring breakdown:')
        print(f'  Patch additions: {len(patch_changes.additions)}')
        print(f'  Patch deletions: {len(patch_changes.deletions)}')
        print(f'  Lines with score: {breakdown.lines_with_score}')
        print(f'  Structural: {breakdown.structural_count} nodes = {breakdown.structural_score:.2f}')
        print(f'  Leaf: {breakdown.leaf_count} tokens = {breakdown.leaf_score:.2f}')
        print(f'  Total score: {breakdown.total_score:.2f}')

    def test_python_new_file_with_docstrings_and_comments(self, weights):
        """
        Test scoring of a new Python file with docstrings and comments.

        Verifies that:
        - Docstrings/comments get 0 score
        - Class and function definitions get structural bonuses
        - lines_with_score excludes comment/docstring lines
        """
        # Simulated new file patch
        patch = '''@@ -0,0 +1,28 @@
+import weakref
+
+
+class WeakMethodCallable:
+    """
+    A callable that holds a weak reference to a bound method.
+    Used to break reference cycles in CachedFetcher.
+    """
+
+    def __init__(self, bound_method):
+        # Store weak reference to avoid cycles
+        self._weak_method = weakref.WeakMethod(bound_method)
+
+    async def __call__(self, *args, **kwargs):
+        method = self._weak_method()
+        if method is None:
+            # Instance was garbage collected
+            return None
+        return await method(*args, **kwargs)
+
+
+def helper_function(x, y):
+    """Simple helper."""
+    # Add the values
+    result = x + y
+    return result'''

        # The actual file content
        file_content = '''import weakref


class WeakMethodCallable:
    """
    A callable that holds a weak reference to a bound method.
    Used to break reference cycles in CachedFetcher.
    """

    def __init__(self, bound_method):
        # Store weak reference to avoid cycles
        self._weak_method = weakref.WeakMethod(bound_method)

    async def __call__(self, *args, **kwargs):
        method = self._weak_method()
        if method is None:
            # Instance was garbage collected
            return None
        return await method(*args, **kwargs)


def helper_function(x, y):
    """Simple helper."""
    # Add the values
    result = x + y
    return result'''

        # Run scoring
        breakdown = calculate_score_with_breakdown(file_content, 'py', weights, patch)
        patch_changes = extract_patch_changes(patch)

        # All 26 non-empty lines are additions (28 total - 2 blank)
        # But blank lines in patch still count
        total_additions = len(patch_changes.additions)
        assert total_additions == 26  # 28 lines but 2 are blank (still counted as additions)

        # Structural elements: class definition, 2 function definitions (def __init__, async def, def helper)
        assert breakdown.structural_count >= 3, (
            f'Expected at least 3 structural elements, got {breakdown.structural_count}'
        )

        # lines_with_score should exclude:
        # - Docstring lines (lines 5-8, line 23)
        # - Comment lines (lines 11, 17, 24)
        # - Blank lines
        # So lines_with_score should be significantly less than total_additions
        assert breakdown.lines_with_score < total_additions, (
            f'lines_with_score ({breakdown.lines_with_score}) should be less than total additions ({total_additions}) due to comments/docstrings'
        )

        # But we should still have meaningful score
        assert breakdown.total_score > 0
        assert breakdown.lines_with_score > 0

        # Verify the import line scored (leaf token)
        assert breakdown.leaf_count > 0

        # Print breakdown for debugging
        print('\nPython new file scoring breakdown:')
        print(f'  Total additions: {total_additions}')
        print(f'  Lines with score: {breakdown.lines_with_score}')
        print(f'  Structural: {breakdown.structural_count} nodes = {breakdown.structural_score:.2f}')
        print(f'  Leaf: {breakdown.leaf_count} tokens = {breakdown.leaf_score:.2f}')
        print(f'  Total score: {breakdown.total_score:.2f}')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
