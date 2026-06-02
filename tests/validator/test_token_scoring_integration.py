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

from gittensor.classes import FileChange, ScoringCategory
from gittensor.utils.github_api_tools import FileContentPair
from gittensor.validator.utils.load_weights import TokenConfig, load_programming_language_weights, load_token_config
from gittensor.validator.utils.tree_sitter_scoring import calculate_token_score_from_file_changes, score_tree_diff


class TestTreeDiffScoring:
    """Integration tests for tree-diff scoring approach."""

    @pytest.fixture
    def weights(self) -> TokenConfig:
        return load_token_config()

    @pytest.mark.parametrize(
        'filename,old_content,new_content',
        [
            (
                'Dockerfile',
                'FROM python:3.12-slim\n',
                'FROM python:3.12-slim\nRUN pip install uv\nCOPY . /app\n',
            ),
            (
                'Makefile',
                'test:\n\tpytest\n',
                'test:\n\tpytest\n\nlint:\n\truff check\n',
            ),
        ],
    )
    def test_configured_extensionless_files_reach_tree_diff(self, weights, filename, old_content, new_content):
        file_change = FileChange(
            pr_number=1,
            repository_full_name='test/repo',
            filename=filename,
            changes=3,
            additions=2,
            deletions=1,
            status='modified',
        )
        result = calculate_token_score_from_file_changes(
            [file_change],
            {filename: FileContentPair(old_content=old_content, new_content=new_content)},
            weights,
            load_programming_language_weights(),
        )

        file_result = result.file_results[0]
        assert file_change.file_extension == filename.lower()
        assert file_result.scoring_method == 'tree-diff'
        assert file_result.nodes_scored > 0

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
        print(
            f'  Structural: +{breakdown.structural_added_count}/-{breakdown.structural_deleted_count} = {breakdown.structural_score:.2f}'
        )
        print(f'  Leaf: +{breakdown.leaf_added_count}/-{breakdown.leaf_deleted_count} = {breakdown.leaf_score:.2f}')
        print(f'  Total score: {breakdown.total_score:.2f}')

    def test_identical_files_score_zero(self, weights):
        """
        Test that identical files score zero.

        No changes = no score.
        """
        content = """def example():
    return 42
"""
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
        old_content = """def process(data):
    return data * 2
"""
        new_content = """# This function processes data
# It multiplies the input by 2
def process(data):
    # Multiply by 2
    return data * 2  # Return the result
"""
        breakdown = score_tree_diff(old_content, new_content, 'py', weights)

        # Should have minimal score (only comments were added)
        # Comments should not contribute to structural or meaningful leaf tokens
        print('\nComment-only changes:')
        print(
            f'  Structural: +{breakdown.structural_added_count}/-{breakdown.structural_deleted_count} = {breakdown.structural_score:.2f}'
        )
        print(f'  Leaf: +{breakdown.leaf_added_count}/-{breakdown.leaf_deleted_count} = {breakdown.leaf_score:.2f}')
        print(f'  Total score: {breakdown.total_score:.2f}')

        # The score should be very low since only comments changed
        # (Some languages may parse comment text as leaf nodes, but they should have 0 weight)

    def test_rust_file_scoring(self, weights):
        """
        Test scoring a Rust file to ensure language support.
        """
        new_content = """impl Calculator {
    fn add(&self, a: i32, b: i32) -> i32 {
        a + b
    }

    fn multiply(&self, a: i32, b: i32) -> i32 {
        a * b
    }
}
"""
        breakdown = score_tree_diff(None, new_content, 'rs', weights)

        # Should have positive score
        assert breakdown.total_score > 0, 'Rust file should have positive score'

        # Should have structural elements (impl block, functions)
        assert breakdown.structural_count > 0, 'Should have structural elements'

        print('\nRust file scoring:')
        print(f'  Structural: {breakdown.structural_count} = {breakdown.structural_score:.2f}')
        print(f'  Leaf: {breakdown.leaf_count} = {breakdown.leaf_score:.2f}')
        print(f'  Total: {breakdown.total_score:.2f}')

    def test_rust_inline_test_marker_in_block_comment_scores_as_source(self, weights):
        """
        Test that docs/examples mentioning #[test] do not demote production edits.
        """
        old_content = 'fn prod() -> i32 { 1 }\n'
        with_marker = 'fn prod() -> i32 { 2 }\n/*\n#[test]\nfn example() {}\n*/\n'
        without_marker = 'fn prod() -> i32 { 2 }\n/*\nExample test function goes here.\n*/\n'
        programming_languages = load_programming_language_weights()

        marker_change = FileChange(
            pr_number=1,
            repository_full_name='owner/repo',
            filename='src/lib.rs',
            changes=2,
            additions=1,
            deletions=1,
            status='modified',
        )
        marker_result = calculate_token_score_from_file_changes(
            [marker_change],
            {'src/lib.rs': FileContentPair(old_content=old_content, new_content=with_marker)},
            weights,
            programming_languages,
        ).file_results[0]

        control_change = FileChange(
            pr_number=1,
            repository_full_name='owner/repo',
            filename='src/lib.rs',
            changes=2,
            additions=1,
            deletions=1,
            status='modified',
        )
        control_result = calculate_token_score_from_file_changes(
            [control_change],
            {'src/lib.rs': FileContentPair(old_content=old_content, new_content=without_marker)},
            weights,
            programming_languages,
        ).file_results[0]

        assert marker_result.category == ScoringCategory.SOURCE
        assert marker_result.is_test_file is False
        assert marker_result.score == pytest.approx(control_result.score)

    def test_typescript_file_scoring(self, weights):
        """
        Test scoring a TypeScript file.
        """
        new_content = """interface User {
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
"""
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
        old_content = """class OldClass:
    def method(self):
        pass
"""
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

    # ------------------------------------------------------------------
    # Golden-value regression tests
    #
    # These pin the exact ScoreBreakdown produced for a small set of stable
    # fixtures, so any change in tree-sitter grammar output, TokenConfig
    # weights, the scoring algorithm, or comment-exclusion behavior is
    # caught here. If one of these fails, identify which of the above
    # changed and update the expected values intentionally.
    # ------------------------------------------------------------------

    def test_pinned_python_simple_function(self, weights):
        breakdown = score_tree_diff(None, 'def foo():\n    return 1\n', 'py', weights)

        assert breakdown.structural_added_count == 2
        assert breakdown.leaf_added_count == 7
        assert breakdown.structural_deleted_count == 0
        assert breakdown.leaf_deleted_count == 0
        assert breakdown.structural_score == pytest.approx(2.35, abs=1e-6)
        assert breakdown.leaf_score == pytest.approx(0.10, abs=1e-6)
        assert breakdown.total_score == pytest.approx(2.45, abs=1e-6)

    def test_pinned_python_empty_file(self, weights):
        breakdown = score_tree_diff(None, '', 'py', weights)

        assert breakdown.structural_added_count == 0
        assert breakdown.leaf_added_count == 0
        assert breakdown.total_score == 0.0

    def test_pinned_python_only_comments(self, weights):
        """Comment-only content scores zero (the walker skips comment subtrees)."""
        content = '# just a comment\n# and another\n'
        breakdown = score_tree_diff(None, content, 'py', weights)

        assert breakdown.structural_added_count == 0
        assert breakdown.leaf_added_count == 0
        assert breakdown.total_score == 0.0

    def test_pinned_python_rename_identifier(self, weights):
        """Renaming an identifier produces one leaf-add and one leaf-delete; structure unchanged."""
        breakdown = score_tree_diff(
            'def foo():\n    return 1\n',
            'def bar():\n    return 1\n',
            'py',
            weights,
        )

        assert breakdown.structural_added_count == 0
        assert breakdown.structural_deleted_count == 0
        assert breakdown.leaf_added_count == 1
        assert breakdown.leaf_deleted_count == 1
        assert breakdown.structural_score == pytest.approx(0.0, abs=1e-6)
        assert breakdown.leaf_score == pytest.approx(0.14, abs=1e-6)
        assert breakdown.total_score == pytest.approx(0.14, abs=1e-6)

    def test_pinned_python_add_statement(self, weights):
        """Adding `x = 1` and changing the return introduces structural + leaf additions."""
        breakdown = score_tree_diff(
            'def foo():\n    return 1\n',
            'def foo():\n    x = 1\n    return x\n',
            'py',
            weights,
        )

        assert breakdown.structural_added_count == 1
        assert breakdown.structural_deleted_count == 0
        assert breakdown.leaf_added_count == 3
        assert breakdown.leaf_deleted_count == 0
        assert breakdown.structural_score == pytest.approx(0.20, abs=1e-6)
        assert breakdown.leaf_score == pytest.approx(0.14, abs=1e-6)
        assert breakdown.total_score == pytest.approx(0.34, abs=1e-6)

    def test_pinned_python_deleted_file_mirrors_new_file(self, weights):
        """Deleting a file produces the same counts/score as adding it."""
        src = 'def foo():\n    return 1\n'
        added = score_tree_diff(None, src, 'py', weights)
        deleted = score_tree_diff(src, None, 'py', weights)

        assert deleted.structural_added_count == 0
        assert deleted.leaf_added_count == 0
        assert deleted.structural_deleted_count == added.structural_added_count
        assert deleted.leaf_deleted_count == added.leaf_added_count
        assert deleted.total_score == pytest.approx(added.total_score, abs=1e-6)

    def test_pinned_python_identical_files_score_zero(self, weights):
        src = 'def foo():\n    return 1\n'
        breakdown = score_tree_diff(src, src, 'py', weights)

        assert breakdown.structural_added_count == 0
        assert breakdown.leaf_added_count == 0
        assert breakdown.structural_deleted_count == 0
        assert breakdown.leaf_deleted_count == 0
        assert breakdown.total_score == 0.0

    def test_pinned_rust_simple_function(self, weights):
        """Rust function has no structural-bonus node type but produces leaf signatures."""
        content = 'fn add(a: i32, b: i32) -> i32 { a + b }\n'
        breakdown = score_tree_diff(None, content, 'rs', weights)

        assert breakdown.structural_added_count == 0
        assert breakdown.leaf_added_count == 18
        assert breakdown.structural_score == pytest.approx(0.0, abs=1e-6)
        assert breakdown.leaf_score == pytest.approx(0.35, abs=1e-6)
        assert breakdown.total_score == pytest.approx(0.35, abs=1e-6)

    def test_pinned_bash_unweighted_leaves(self, weights):
        """Bash 'word' nodes produce leaf signatures but carry zero leaf weight - score zero."""
        breakdown = score_tree_diff(None, 'echo hello\necho world\n', 'sh', weights)

        assert breakdown.leaf_added_count == 4
        assert breakdown.total_score == 0.0

    def test_pinned_rust_struct_with_impl(self, weights):
        """Rust struct + impl with multiple methods, match, let, generics."""
        content = """//! Module-level doc.
use std::collections::HashMap;

pub struct Counter {
    map: HashMap<String, u64>,
}

impl Counter {
    pub fn new() -> Self {
        Self { map: HashMap::new() }
    }

    pub fn add(&mut self, key: String) -> u64 {
        let count = self.map.entry(key).or_insert(0);
        *count += 1;
        *count
    }

    pub fn get(&self, key: &str) -> u64 {
        match self.map.get(key) {
            Some(&n) => n,
            None => 0,
        }
    }
}
"""
        breakdown = score_tree_diff(None, content, 'rs', weights)

        assert breakdown.structural_added_count == 7
        assert breakdown.leaf_added_count == 123
        assert breakdown.structural_score == pytest.approx(4.45, abs=1e-6)
        assert breakdown.leaf_score == pytest.approx(3.56, abs=1e-6)
        assert breakdown.total_score == pytest.approx(8.01, abs=1e-6)

    def test_pinned_rust_add_impl_method(self, weights):
        """Adding a new impl block with one method produces incremental structural + leaf adds."""
        old = """pub struct Counter {
    map: std::collections::HashMap<String, u64>,
}

impl Counter {
    pub fn new() -> Self {
        Self { map: std::collections::HashMap::new() }
    }
}
"""
        new = (
            old
            + """
impl Counter {
    pub fn remove(&mut self, key: &str) -> Option<u64> {
        self.map.remove(key)
    }
}
"""
        )
        breakdown = score_tree_diff(old, new, 'rs', weights)

        assert breakdown.structural_added_count == 2
        assert breakdown.structural_deleted_count == 0
        assert breakdown.leaf_added_count == 32
        assert breakdown.leaf_deleted_count == 0
        assert breakdown.total_score == pytest.approx(3.25, abs=1e-6)

    def test_pinned_typescript_interface_class(self, weights):
        """TypeScript interface + class with map field, methods, and conditional logic."""
        content = """interface User {
    id: number;
    name: string;
}

class UserRegistry {
    private users: Map<number, User> = new Map();

    addUser(user: User): boolean {
        if (this.users.has(user.id)) {
            return false;
        }
        this.users.set(user.id, user);
        return true;
    }

    findById(id: number): User | undefined {
        return this.users.get(id);
    }
}
"""
        breakdown = score_tree_diff(None, content, 'ts', weights)

        assert breakdown.structural_added_count == 12
        assert breakdown.leaf_added_count == 97
        assert breakdown.structural_score == pytest.approx(11.70, abs=1e-6)
        assert breakdown.leaf_score == pytest.approx(3.17, abs=1e-6)
        assert breakdown.total_score == pytest.approx(14.87, abs=1e-6)

    def test_pinned_typescript_method_rename(self, weights):
        """Renaming methods produces only leaf-level diffs (structure preserved)."""
        old = """class Registry {
    addUser(id: number): boolean { return true; }
    findById(id: number): number { return id; }
}
"""
        new = """class Registry {
    register(id: number): boolean { return true; }
    lookup(id: number): number { return id; }
}
"""
        breakdown = score_tree_diff(old, new, 'ts', weights)

        assert breakdown.structural_added_count == 0
        assert breakdown.structural_deleted_count == 0
        assert breakdown.leaf_added_count == 2
        assert breakdown.leaf_deleted_count == 2
        assert breakdown.structural_score == pytest.approx(0.0, abs=1e-6)
        assert breakdown.leaf_score == pytest.approx(0.28, abs=1e-6)
        assert breakdown.total_score == pytest.approx(0.28, abs=1e-6)

    def test_pinned_python_control_flow(self, weights):
        """Python with rich control flow (for / if / elif / else / try / except / with /
        raise / return / augmented_assignment) exercises many structural-bonus node
        types in a single fixture.
        """
        content = """def process(items):
    results = []
    for item in items:
        if item < 0:
            continue
        elif item == 0:
            results.append(None)
        else:
            try:
                with open(f"item_{item}.txt") as f:
                    results.append(f.read())
            except FileNotFoundError:
                results.append("missing")
            except OSError as e:
                raise RuntimeError(f"io error: {e}") from e
    return results
"""
        breakdown = score_tree_diff(None, content, 'py', weights)

        assert breakdown.structural_added_count == 22
        assert breakdown.leaf_added_count == 90
        assert breakdown.structural_deleted_count == 0
        assert breakdown.leaf_deleted_count == 0
        assert breakdown.structural_score == pytest.approx(8.00, abs=1e-6)
        assert breakdown.leaf_score == pytest.approx(1.99, abs=1e-6)
        assert breakdown.total_score == pytest.approx(9.99, abs=1e-6)

    # ------------------------------------------------------------------
    # Missing base content guard tests
    #
    # A non-added file (modified / renamed) whose base blob is binary,
    # oversized, or unavailable arrives with old_content=None from the
    # das-github-mirror fetcher. Without a guard the scorer falls through
    # to tree-diff and treats the entire head file as net-new additions,
    # inflating the score. The guard must skip these files. Added files
    # with old_content=None are legitimate new-file additions and must
    # continue to be scored via tree-diff.
    # ------------------------------------------------------------------

    def test_modified_file_missing_base_content_is_skipped(self, weights):
        """Modified file with old_content=None must be skipped, not scored as new."""
        new_content = 'def fn():\n    return 1\n'
        file_change = FileChange(
            pr_number=1,
            repository_full_name='owner/repo',
            filename='src/lib.py',
            changes=10,
            additions=10,
            deletions=5,
            status='modified',
        )
        result = calculate_token_score_from_file_changes(
            [file_change],
            {'src/lib.py': FileContentPair(old_content=None, new_content=new_content)},
            weights,
            load_programming_language_weights(),
        )
        file_result = result.file_results[0]
        assert file_result.scoring_method == 'skipped-missing-base'
        assert file_result.score == 0.0
        assert file_result.nodes_scored == 0
        assert result.total_score == 0.0

    def test_renamed_file_missing_base_content_is_skipped(self, weights):
        """Renamed file with old_content=None must be skipped, not scored as new."""
        new_content = 'def fn():\n    return 1\n'
        file_change = FileChange(
            pr_number=1,
            repository_full_name='owner/repo',
            filename='new_name.py',
            changes=5,
            additions=5,
            deletions=3,
            status='renamed',
            previous_filename='old_name.py',
        )
        result = calculate_token_score_from_file_changes(
            [file_change],
            {'new_name.py': FileContentPair(old_content=None, new_content=new_content)},
            weights,
            load_programming_language_weights(),
        )
        file_result = result.file_results[0]
        assert file_result.scoring_method == 'skipped-missing-base'
        assert file_result.score == 0.0
        assert file_result.nodes_scored == 0
        assert result.total_score == 0.0

    def test_added_file_null_old_content_scores_as_new_file(self, weights):
        """Added file with old_content=None is correct; must still score via tree-diff."""
        new_content = 'def fn():\n    return 1\n'
        file_change = FileChange(
            pr_number=1,
            repository_full_name='owner/repo',
            filename='new_file.py',
            changes=2,
            additions=2,
            deletions=0,
            status='added',
        )
        result = calculate_token_score_from_file_changes(
            [file_change],
            {'new_file.py': FileContentPair(old_content=None, new_content=new_content)},
            weights,
            load_programming_language_weights(),
        )
        file_result = result.file_results[0]
        assert file_result.scoring_method == 'tree-diff'
        assert file_result.score > 0
        assert file_result.nodes_scored > 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
