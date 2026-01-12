# The MIT License (MIT)
# Copyright © 2025 Entrius
from collections import Counter
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union

import bittensor as bt
from tree_sitter import Node, Parser, Tree

from gittensor.classes import (
    FileScoreResult,
    PrScoringResult,
    ScoreBreakdown,
)
from gittensor.constants import (
    LOW_VALUE_SIZE_MEDIUM,
    LOW_VALUE_SIZE_SMALL,
    LOW_VALUE_THRESHOLD_LARGE,
    LOW_VALUE_THRESHOLD_MEDIUM,
    LOW_VALUE_THRESHOLD_SMALL,
    MAX_FILE_SIZE_BYTES,
    MAX_LINES_SCORED_FOR_NON_CODE_EXT,
    NON_CODE_EXTENSIONS,
    TEST_FILE_CONTRIBUTION_WEIGHT,
)
from gittensor.utils.github_api_tools import FileContentPair
from gittensor.validator.utils.load_weights import LanguageConfig, TokenConfig

if TYPE_CHECKING:
    from gittensor.classes import FileChange


# Cache parsers to avoid repeated initialization
_parser_cache: Dict[str, Parser] = {}


def get_parser(language: str) -> Optional[Parser]:
    """
    Get a tree-sitter parser for the given language.

    Args:
        language: Tree-sitter language name (e.g., 'python', 'javascript')

    Returns:
        Parser instance or None if language not supported
    """
    if language in _parser_cache:
        return _parser_cache[language]

    try:
        from tree_sitter_language_pack import get_parser as get_ts_parser

        parser = get_ts_parser(language)
        _parser_cache[language] = parser
        return parser
    except Exception as e:
        bt.logging.debug(f'Failed to get parser for {language}: {e}')
        return None


def parse_code(content: str, language: str) -> Optional[Tree]:
    """
    Parse source code into a tree-sitter AST.

    Args:
        content: Source code as string
        language: Tree-sitter language name

    Returns:
        Tree object or None if parsing failed
    """
    parser = get_parser(language)
    if not parser:
        return None

    try:
        return parser.parse(content.encode('utf-8'))
    except Exception as e:
        bt.logging.debug(f'Failed to parse code: {e}')
        return None


# =============================================================================
# Tree Diff Scoring
# =============================================================================

# Node types that represent comments (always score 0)
COMMENT_NODE_TYPES = frozenset(
    {
        'comment',
        'line_comment',
        'block_comment',
        'documentation_comment',
        'doc_comment',
    }
)

# Type alias for node signatures
# Structural: ("structural", node_type)
# Leaf: ("leaf", node_type, text)
NodeSignature = Union[Tuple[str, str], Tuple[str, str, str]]


def is_comment_node(node: Node) -> bool:
    """Check if a node is a comment."""
    return node.type in COMMENT_NODE_TYPES


def collect_node_signatures(
    tree: Tree,
    weights: TokenConfig,
) -> Counter[NodeSignature]:
    """
    Collect node signatures from an AST for tree diff comparison.

    Walks the tree and collects signatures for:
    - Structural nodes: ("structural", node_type) - captures structure without content
    - Leaf nodes: ("leaf", node_type, text) - captures content for meaningful tokens

    Comments are skipped entirely (score 0).

    Args:
        tree: Parsed tree-sitter AST
        weights: TokenConfig instance with structural_bonus definitions

    Returns:
        Counter of node signatures (multiset for handling duplicates)
    """
    signatures: Counter[NodeSignature] = Counter()

    def walk_node(node: Node) -> None:
        # Skip comments entirely
        if is_comment_node(node):
            return

        node_type = node.type

        # Check if this is a structural node (has structural bonus)
        if weights.get_structural_weight(node_type) > 0:
            # Structural signature: only type matters (not content)
            signatures[('structural', node_type)] += 1

        # Check if this is a leaf node
        if node.child_count == 0:
            # Skip comment types at leaf level too
            if node_type not in COMMENT_NODE_TYPES:
                # Leaf signature: type + text content
                text = node.text.decode('utf-8') if node.text else ''
                signatures[('leaf', node_type, text)] += 1

        # Recurse into children
        for child in node.children:
            walk_node(child)

    walk_node(tree.root_node)
    return signatures


def score_tree_diff(
    old_content: Optional[str],
    new_content: Optional[str],
    extension: str,
    weights: TokenConfig,
) -> ScoreBreakdown:
    """
    Calculate score by comparing old and new file ASTs using symmetric difference.

    - Structural nodes are identified by type only (moving code around = no change)
    - Leaf nodes are identified by type + content (actual token changes)
    - Both additions (in new but not old) and deletions (in old but not new) are scored

    Args:
        old_content: Content of the file before changes (None for new files)
        new_content: Content of the file after changes (None for deleted files)
        extension: File extension for language detection
        weights: TokenConfig instance with scoring configuration

    Returns:
        ScoreBreakdown with added/deleted counts and scores for structural/leaf nodes
    """
    breakdown = ScoreBreakdown()

    language = weights.get_language(extension)
    if not language:
        return breakdown

    # Parse both versions
    old_signatures: Counter[NodeSignature] = Counter()
    new_signatures: Counter[NodeSignature] = Counter()

    if old_content:
        old_tree = parse_code(old_content, language)
        if old_tree:
            old_signatures = collect_node_signatures(old_tree, weights)

    if new_content:
        new_tree = parse_code(new_content, language)
        if new_tree:
            new_signatures = collect_node_signatures(new_tree, weights)

    # Compute symmetric difference using Counter subtraction
    # added = elements in new but not in old (or more copies in new)
    # deleted = elements in old but not in new (or more copies in old)
    added = new_signatures - old_signatures
    deleted = old_signatures - new_signatures

    # Score added nodes
    for signature, count in added.items():
        if signature[0] == 'structural':
            _, node_type = signature
            weight = weights.get_structural_weight(node_type)
            breakdown.structural_added_count += count
            breakdown.structural_added_score += weight * count
        else:  # leaf
            _, node_type, _ = signature
            weight = weights.get_leaf_weight(node_type)
            breakdown.leaf_added_count += count
            breakdown.leaf_added_score += weight * count

    # Score deleted nodes
    for signature, count in deleted.items():
        if signature[0] == 'structural':
            _, node_type = signature
            weight = weights.get_structural_weight(node_type)
            breakdown.structural_deleted_count += count
            breakdown.structural_deleted_score += weight * count
        else:  # leaf
            _, node_type, _ = signature
            weight = weights.get_leaf_weight(node_type)
            breakdown.leaf_deleted_count += count
            breakdown.leaf_deleted_score += weight * count

    return breakdown


# =============================================================================
# Low-Value PR Detection
# =============================================================================


def get_low_value_threshold(total_raw_lines: int) -> float:
    """Get the score-per-line threshold for low-value PR detection.

    Uses tiered thresholds based on PR size:
    - Small PRs (<20 lines): Higher threshold (0.5) - should be dense, meaningful code
    - Medium PRs (20-100 lines): Medium threshold (0.35)
    - Large PRs (>100 lines): Lower threshold (0.25) - naturally include mixed content

    This prevents miners from padding small PRs with low-value content to meet
    a single threshold.

    Args:
        total_raw_lines: Total lines changed (additions + deletions) from git stats

    Returns:
        Score-per-line threshold for this PR size
    """
    if total_raw_lines < LOW_VALUE_SIZE_SMALL:
        return LOW_VALUE_THRESHOLD_SMALL
    elif total_raw_lines < LOW_VALUE_SIZE_MEDIUM:
        return LOW_VALUE_THRESHOLD_MEDIUM
    else:
        return LOW_VALUE_THRESHOLD_LARGE


def is_low_value_pr(total_score: float, total_raw_lines: int) -> bool:
    """Determine if a PR is low-value based on score-per-line with tiered thresholds.

    Low-value PRs are those with an average token score per line below
    the threshold for their size tier. This catches:
    - PRs with mostly whitespace/comments (low score per line)
    - PRs that pad with low-value content to meet requirements

    Args:
        total_score: Total token score for the PR
        total_raw_lines: Total lines changed (additions + deletions) from git stats

    Returns:
        True if PR is low-value, False otherwise
    """
    if total_raw_lines == 0:
        return True

    score_per_line = total_score / total_raw_lines
    threshold = get_low_value_threshold(total_raw_lines)

    return score_per_line < threshold


# =============================================================================
# Main Scoring Pipeline
# =============================================================================


def calculate_token_score_from_file_changes(
    file_changes: List['FileChange'],
    file_contents: Dict[str, FileContentPair],
    weights: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
) -> PrScoringResult:
    """
    Calculate contribution score using tree-sitter AST comparison.

    Compares old and new file ASTs to score meaningful changes:
    - Structural nodes (functions, classes, etc.) identified by type only
    - Leaf nodes (identifiers, literals) identified by type + content
    - Both additions and deletions are scored

    Args:
        file_changes: List of FileChange objects from the PR
        file_contents: Dict mapping file paths to FileContentPair(old_content, new_content)
        weights: TokenConfig instance with scoring configuration
        programming_languages: Language weight mapping (for fallback/documentation files)

    Returns:
        PrScoringResult with total score, low-value flag, and per-file details
    """
    if not file_changes:
        return PrScoringResult(
            total_score=0.0,
            is_low_value_pr=True,
            total_nodes_scored=0,
            file_results=[],
        )

    file_results: List[FileScoreResult] = []
    total_score = 0.0
    total_nodes_scored = 0

    for file in file_changes:
        ext = file.file_extension
        is_test_file = file.is_test_file()
        file_weight = TEST_FILE_CONTRIBUTION_WEIGHT if is_test_file else 1.0

        # Skip deleted files (status == 'removed')
        if file.status == 'removed':
            file_results.append(
                FileScoreResult(
                    filename=file.short_name,
                    score=0.0,
                    nodes_scored=0,
                    total_lines=file.deletions,
                    is_test_file=is_test_file,
                    scoring_method='skipped',
                )
            )
            continue

        # Handle mitigated extensions early - no content fetch needed
        if ext in NON_CODE_EXTENSIONS:
            lines_to_score = min(file.changes, MAX_LINES_SCORED_FOR_NON_CODE_EXT)
            lang_config = programming_languages.get(ext)
            lang_weight = lang_config.weight if lang_config else 0.01
            file_score = lang_weight * lines_to_score * file_weight

            total_score += file_score
            total_nodes_scored += lines_to_score

            file_results.append(
                FileScoreResult(
                    filename=file.short_name,
                    score=file_score,
                    nodes_scored=lines_to_score,
                    total_lines=file.changes,
                    is_test_file=is_test_file,
                    scoring_method='line-count',
                )
            )
            continue

        # Get file content pair (old and new versions)
        content_pair = file_contents.get(file.filename)

        # Handle missing/binary files - score 0
        if content_pair is None or content_pair.new_content is None:
            bt.logging.debug(f'  │   {file.short_name}: skipped (binary or fetch failed)')
            file_results.append(
                FileScoreResult(
                    filename=file.short_name,
                    score=0.0,
                    nodes_scored=0,
                    total_lines=file.changes,
                    is_test_file=is_test_file,
                    scoring_method='skipped-binary',
                )
            )
            continue

        # Extract old and new content for tree comparison
        old_content = content_pair.old_content  # None for new files
        new_content = content_pair.new_content

        # Check file size - score 0 for large files
        if len(new_content.encode('utf-8')) > MAX_FILE_SIZE_BYTES:
            bt.logging.debug(f'  │   {file.short_name}: skipped (file too large, >{MAX_FILE_SIZE_BYTES} bytes)')
            file_results.append(
                FileScoreResult(
                    filename=file.short_name,
                    score=0.0,
                    nodes_scored=0,
                    total_lines=file.changes,
                    is_test_file=is_test_file,
                    scoring_method='skipped-large',
                )
            )
            continue

        # Check if tree-sitter supports this extension
        if not weights.supports_tree_sitter(ext):
            bt.logging.debug(f'  │   {file.short_name}: skipped (extension .{ext} not supported)')
            file_results.append(
                FileScoreResult(
                    filename=file.short_name,
                    score=0.0,
                    nodes_scored=0,
                    total_lines=file.changes,
                    is_test_file=is_test_file,
                    scoring_method='skipped-unsupported',
                )
            )
            continue

        # Use tree diff scoring - compare old and new ASTs
        file_breakdown = score_tree_diff(old_content, new_content, ext, weights)
        scoring_method = 'tree-diff'

        # Apply test file weight
        file_breakdown = file_breakdown.with_weight(file_weight)
        file_score = file_breakdown.total_score

        # Track nodes scored for this file
        nodes_scored = file_breakdown.added_count + file_breakdown.deleted_count

        total_score += file_score
        total_nodes_scored += nodes_scored

        file_results.append(
            FileScoreResult(
                filename=file.short_name,
                score=file_score,
                nodes_scored=nodes_scored,
                total_lines=file.changes,
                is_test_file=is_test_file,
                scoring_method=scoring_method,
                breakdown=file_breakdown,
            )
        )

    # Compute total raw lines (additions + deletions) for low-value detection
    total_raw_lines = sum(f.total_lines for f in file_results)

    # Determine if this is a low-value PR using tiered thresholds
    low_value = is_low_value_pr(total_score, total_raw_lines)

    # Compute aggregate breakdown from file_results
    breakdowns = [r.breakdown for r in file_results if r.breakdown is not None]
    total_breakdown = sum(breakdowns, start=ScoreBreakdown()) if breakdowns else None

    _log_scoring_results(
        file_results,
        total_score,
        total_raw_lines,
        low_value,
        total_breakdown,
    )

    return PrScoringResult(
        total_score=total_score,
        is_low_value_pr=low_value,
        total_nodes_scored=total_nodes_scored,
        file_results=file_results,
        score_breakdown=total_breakdown,
    )


def _log_scoring_results(
    file_results: List[FileScoreResult],
    total_score: float,
    total_raw_lines: int,
    low_value: bool,
    breakdown: Optional[ScoreBreakdown] = None,
) -> None:
    """Log scoring results for debugging."""
    bt.logging.debug(f'  ├─ Files ({len(file_results)} scored):')

    if file_results:
        max_name_len = max(len(f.filename) for f in file_results)
        for result in file_results:
            test_mark = ' [test]' if result.is_test_file else ''
            method_mark = f' ({result.scoring_method})'
            bt.logging.debug(
                f'  │   {result.filename:<{max_name_len}}  '
                f'{result.nodes_scored:>3} nodes  '
                f'{result.score:>6.2f}{test_mark}{method_mark}'
            )

    # Count files by scoring method
    line_count_files = [f for f in file_results if f.scoring_method == 'line-count']
    line_count_score = sum(f.score for f in line_count_files)

    # Build score breakdown string showing added vs deleted
    breakdown_parts = []
    if breakdown:
        # Structural breakdown
        if breakdown.structural_count > 0:
            struct_parts = []
            if breakdown.structural_added_count > 0:
                struct_parts.append(f'+{breakdown.structural_added_count}')
            if breakdown.structural_deleted_count > 0:
                struct_parts.append(f'-{breakdown.structural_deleted_count}')
            struct_str = '/'.join(struct_parts) if struct_parts else str(breakdown.structural_count)
            breakdown_parts.append(f'Struct: {struct_str} = {breakdown.structural_score:.2f}')

        # Leaf breakdown
        if breakdown.leaf_count > 0:
            leaf_parts = []
            if breakdown.leaf_added_count > 0:
                leaf_parts.append(f'+{breakdown.leaf_added_count}')
            if breakdown.leaf_deleted_count > 0:
                leaf_parts.append(f'-{breakdown.leaf_deleted_count}')
            leaf_str = '/'.join(leaf_parts) if leaf_parts else str(breakdown.leaf_count)
            breakdown_parts.append(f'Leaf: {leaf_str} = {breakdown.leaf_score:.2f}')

    # Add line-count info if there were line-count scored files
    if line_count_files:
        line_count_lines = sum(f.nodes_scored for f in line_count_files)
        breakdown_parts.append(
            f'Line-count: {len(line_count_files)} files, {line_count_lines} lines = {line_count_score:.2f}'
        )

    breakdown_str = ' | '.join(breakdown_parts) if breakdown_parts else ''

    score_per_line = total_score / total_raw_lines if total_raw_lines > 0 else 0
    threshold = get_low_value_threshold(total_raw_lines)
    low_value_str = ' [LOW VALUE]' if low_value else ''
    bt.logging.info(
        f'  ├─ Token Score: {total_score:.2f} | '
        f'Lines: {total_raw_lines} | Score/Line: {score_per_line:.2f} (threshold: {threshold}){low_value_str}'
    )

    if breakdown_str:
        bt.logging.info(f'  │   └─ Breakdown: {breakdown_str}')
