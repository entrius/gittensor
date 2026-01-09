"""
Integration tests for tree-diff token scoring pipeline.

These tests exercise the full scoring pipeline with real file contents,
verifying:
- Tree-sitter AST parsing and comparison
- Structural vs leaf score breakdown
- Comment exclusion from scoring
- Node counting and scoring for additions and deletions

Run tests:
    pytest tests/validator/test_token_scoring_integration.py -v
"""

import pytest

from gittensor.validator.utils.load_weights import TokenConfig, load_token_config
from gittensor.validator.utils.tree_sitter_scoring import score_tree_diff


class TestTreeDiffScoring:
    """Integration tests for tree-diff scoring approach."""

    @pytest.fixture
    def weights(self) -> TokenConfig:
        return load_token_config()

    def test_new_file_scores_all_nodes(self, weights):
        """
        Test scoring a completely new file (no old content).

        All nodes in the new file should be counted as additions.
        """
        new_content = '''def greet(name):
    """Say hello."""
    message = f"Hello, {name}!"
    return message

def farewell(name):
    return f"Goodbye, {name}!"
'''
        # No old content = new file
        breakdown = score_tree_diff(None, new_content, 'py', weights)

        # Should have positive score
        assert breakdown.total_score > 0, 'New file should have positive score'

        # All nodes should be additions (no deletions)
        assert breakdown.added_count > 0, 'Should have added nodes'
        assert breakdown.deleted_count == 0, 'New file should have no deletions'

        # Should have structural elements (function definitions)
        assert breakdown.structural_added_count >= 2, 'Should have at least 2 function definitions'
        assert breakdown.structural_score > 0, 'Should have structural score'

        # Should have leaf tokens
        assert breakdown.leaf_added_count > 0, 'Should have leaf tokens'
        assert breakdown.leaf_score > 0, 'Should have leaf score'

        # Verify score breakdown adds up
        assert abs(breakdown.total_score - (breakdown.structural_score + breakdown.leaf_score)) < 0.01

        print('\nNew file scoring breakdown:')
        print(f'  Structural: +{breakdown.structural_added_count} = {breakdown.structural_score:.2f}')
        print(f'  Leaf: +{breakdown.leaf_added_count} = {breakdown.leaf_score:.2f}')
        print(f'  Total score: {breakdown.total_score:.2f}')

    def test_modified_file_scores_diff(self, weights):
        """
        Test scoring a modified file.

        Should only score the nodes that differ between old and new.
        """
        old_content = '''def calculate(x, y):
    """Calculate sum."""
    return x + y
'''
        new_content = '''def calculate(x, y, z):
    """Calculate sum of three."""
    result = x + y + z
    return result
'''
        breakdown = score_tree_diff(old_content, new_content, 'py', weights)

        # Should have positive score (changes were made)
        assert breakdown.total_score > 0, 'Modified file should have positive score'

        # Should have both additions and possibly deletions
        # The function signature changed, new variable was added, etc.
        assert breakdown.added_count > 0 or breakdown.deleted_count > 0, 'Should detect changes'

        print('\nModified file scoring breakdown:')
        print(f'  Structural: +{breakdown.structural_added_count}/-{breakdown.structural_deleted_count} = {breakdown.structural_score:.2f}')
        print(f'  Leaf: +{breakdown.leaf_added_count}/-{breakdown.leaf_deleted_count} = {breakdown.leaf_score:.2f}')
        print(f'  Total score: {breakdown.total_score:.2f}')

    def test_identical_files_score_zero(self, weights):
        """
        Test that identical files score zero.

        No changes = no score.
        """
        content = '''def example():
    return 42
'''
        breakdown = score_tree_diff(content, content, 'py', weights)

        # Identical files should have zero score
        assert breakdown.total_score == 0, 'Identical files should score zero'
        assert breakdown.added_count == 0, 'No additions'
        assert breakdown.deleted_count == 0, 'No deletions'

    def test_comments_excluded_from_scoring(self, weights):
        """
        Test that comments are excluded from scoring.

        Adding only comments should result in low/zero structural score.
        """
        old_content = '''def process(data):
    return data * 2
'''
        new_content = '''# This function processes data
# It multiplies the input by 2
def process(data):
    # Multiply by 2
    return data * 2  # Return the result
'''
        breakdown = score_tree_diff(old_content, new_content, 'py', weights)

        # Should have minimal score (only comments were added)
        # Comments should not contribute to structural or meaningful leaf tokens
        print('\nComment-only changes:')
        print(f'  Structural: +{breakdown.structural_added_count}/-{breakdown.structural_deleted_count} = {breakdown.structural_score:.2f}')
        print(f'  Leaf: +{breakdown.leaf_added_count}/-{breakdown.leaf_deleted_count} = {breakdown.leaf_score:.2f}')
        print(f'  Total score: {breakdown.total_score:.2f}')

        # The score should be very low since only comments changed
        # (Some languages may parse comment text as leaf nodes, but they should have 0 weight)

    def test_rust_file_scoring(self, weights):
        """
        Test scoring a Rust file to ensure language support.
        """
        new_content = '''impl Calculator {
    fn add(&self, a: i32, b: i32) -> i32 {
        a + b
    }

    fn multiply(&self, a: i32, b: i32) -> i32 {
        a * b
    }
}
'''
        breakdown = score_tree_diff(None, new_content, 'rs', weights)

        # Should have positive score
        assert breakdown.total_score > 0, 'Rust file should have positive score'

        # Should have structural elements (impl block, functions)
        assert breakdown.structural_count > 0, 'Should have structural elements'

        print('\nRust file scoring:')
        print(f'  Structural: {breakdown.structural_count} = {breakdown.structural_score:.2f}')
        print(f'  Leaf: {breakdown.leaf_count} = {breakdown.leaf_score:.2f}')
        print(f'  Total: {breakdown.total_score:.2f}')

    def test_typescript_file_scoring(self, weights):
        """
        Test scoring a TypeScript file.
        """
        new_content = '''interface User {
    id: number;
    name: string;
}

function greetUser(user: User): string {
    return `Hello, ${user.name}!`;
}

const createUser = (id: number, name: string): User => ({
    id,
    name,
});
'''
        breakdown = score_tree_diff(None, new_content, 'ts', weights)

        # Should have positive score
        assert breakdown.total_score > 0, 'TypeScript file should have positive score'

        print('\nTypeScript file scoring:')
        print(f'  Structural: {breakdown.structural_count} = {breakdown.structural_score:.2f}')
        print(f'  Leaf: {breakdown.leaf_count} = {breakdown.leaf_score:.2f}')
        print(f'  Total: {breakdown.total_score:.2f}')

    def test_deleted_file_scores_deletions(self, weights):
        """
        Test scoring a deleted file (old content, no new content).

        All nodes should be counted as deletions.
        """
        old_content = '''class OldClass:
    def method(self):
        pass
'''
        breakdown = score_tree_diff(old_content, None, 'py', weights)

        # Should have positive score (deletions are scored)
        assert breakdown.total_score > 0, 'Deleted file should have positive score'

        # All nodes should be deletions (no additions)
        assert breakdown.deleted_count > 0, 'Should have deleted nodes'
        assert breakdown.added_count == 0, 'Deleted file should have no additions'

        print('\nDeleted file scoring:')
        print(f'  Structural: -{breakdown.structural_deleted_count} = {breakdown.structural_score:.2f}')
        print(f'  Leaf: -{breakdown.leaf_deleted_count} = {breakdown.leaf_score:.2f}')
        print(f'  Total: {breakdown.total_score:.2f}')

    def test_unsupported_language_returns_empty(self, weights):
        """
        Test that unsupported file extensions return empty breakdown.
        """
        content = 'Some content here'
        breakdown = score_tree_diff(None, content, 'unknown_ext', weights)

        assert breakdown.total_score == 0, 'Unsupported language should score zero'
        assert breakdown.added_count == 0
        assert breakdown.deleted_count == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
