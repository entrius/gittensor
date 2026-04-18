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
    ScoringCategory,
)
from gittensor.constants import (
    COMMENT_NODE_TYPES,
    DEFAULT_PROGRAMMING_LANGUAGE_WEIGHT,
    INLINE_TEST_EXTENSIONS,
    INLINE_TEST_PATTERNS,
    MAX_FILE_SIZE_BYTES,
    MAX_LINES_SCORED_FOR_NON_CODE_EXT,
    NON_CODE_EXTENSIONS,
    TEST_FILE_CONTRIBUTION_WEIGHT,
)
from gittensor.utils.github_api_tools import FileContentPair
from gittensor.utils.logging import log_scoring_results
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

        parser = get_ts_parser(language)  # type: ignore[arg-type]
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


def has_inline_tests(content: str, extension: str) -> bool:
    """Check whether source code contains inline test markers.

    Uses simple pattern matching to detect language-specific test constructs
    that live inside production source files.  Currently supports:
    - Rust: ``#[cfg(test)]``, ``#![cfg(test)]``, ``#[test]``, ``#[tokio::test]``
    - Zig:  ``test "name" { ... }``, ``test { ... }``
    - D:    ``unittest { ... }``
    """
    pattern = INLINE_TEST_PATTERNS.get(extension)
    if pattern is None:
        return False
    return pattern.search(content) is not None


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
    added = new_signatures - old_signatures
    deleted = old_signatures - new_signatures

    # Score added nodes
    for signature, count in added.items():
        if signature[0] == 'structural':
            node_type = signature[1]
            weight = weights.get_structural_weight(node_type)
            breakdown.structural_added_count += count
            breakdown.structural_added_score += weight * count
        else:  # leaf
            node_type = signature[1]
            weight = weights.get_leaf_weight(node_type)
            breakdown.leaf_added_count += count
            breakdown.leaf_added_score += weight * count

    # Score deleted nodes
    for signature, count in deleted.items():
        if signature[0] == 'structural':
            node_type = signature[1]
            weight = weights.get_structural_weight(node_type)
            breakdown.structural_deleted_count += count
            breakdown.structural_deleted_score += weight * count
        else:  # leaf
            node_type = signature[1]
            weight = weights.get_leaf_weight(node_type)
            breakdown.leaf_deleted_count += count
            breakdown.leaf_deleted_score += weight * count

    return breakdown

def _score_non_code_file(
    file: FileChange,
    ext: str,
    programming_languages: dict,
    file_weight: float,
    is_test_file: bool,
) -> FileScoreResult:
    lines_to_score = min(file.changes, MAX_LINES_SCORED_FOR_NON_CODE_EXT)
    lang_config = programming_languages.get(ext)
    lang_weight = lang_config.weight if lang_config else DEFAULT_PROGRAMMING_LANGUAGE_WEIGHT
    return FileScoreResult(
        filename=file.short_name,
        score=lang_weight * lines_to_score * file_weight,
        nodes_scored=lines_to_score,
        total_lines=file.changes,
        is_test_file=is_test_file,
        scoring_method='line-count',
    )


def _score_code_file(
    file: FileChange,
    ext: str,
    content_pair,
    weights,
    programming_languages: dict,
    file_weight: float,
    is_test_file: bool,
) -> FileScoreResult:
    if content_pair is None or content_pair.new_content is None:
        bt.logging.debug(f'  │   {file.short_name}: skipped (binary or fetch failed)')
        return FileScoreResult(
            filename=file.short_name,
            score=0.0,
            nodes_scored=0,
            total_lines=file.changes,
            is_test_file=is_test_file,
            scoring_method='skipped-binary',
        )
    if len(content_pair.new_content.encode('utf-8')) > MAX_FILE_SIZE_BYTES:
        bt.logging.debug(f'  │   {file.short_name}: skipped (file too large, >{MAX_FILE_SIZE_BYTES} bytes)')
        return FileScoreResult(
            filename=file.short_name,
            score=0.0,
            nodes_scored=0,
            total_lines=file.changes,
            is_test_file=is_test_file,
            scoring_method='skipped-large',
        )
    if not weights.supports_tree_sitter(ext):
        bt.logging.debug(f'  │   {file.short_name}: skipped (extension .{ext} not supported)')
        return FileScoreResult(
            filename=file.short_name,
            score=0.0,
            nodes_scored=0,
            total_lines=file.changes,
            is_test_file=is_test_file,
            scoring_method='skipped-unsupported',
        )
    old_content = content_pair.old_content
    new_content = content_pair.new_content
    file_breakdown = score_tree_diff(old_content, new_content, ext, weights)
    lang_config = programming_languages.get(ext)
    lang_weight = lang_config.weight if lang_config else 1.0
    if not is_test_file and ext in INLINE_TEST_EXTENSIONS:
        if has_inline_tests(new_content, ext):
            is_test_file = True
            file_weight = TEST_FILE_CONTRIBUTION_WEIGHT
    combined_weight = lang_weight * file_weight
    file_breakdown = file_breakdown.with_weight(combined_weight)
    nodes_scored = file_breakdown.added_count + file_breakdown.deleted_count
    return FileScoreResult(
        filename=file.short_name,
        score=file_breakdown.total_score,
        nodes_scored=nodes_scored,
        total_lines=file.changes,
        is_test_file=is_test_file,
        scoring_method='tree-diff',
        breakdown=file_breakdown,
    )


def _build_category_results(
    cat_files: dict,
    cat_score: dict,
    cat_nodes: dict,
    cat_lines: dict,
    cat_breakdowns: dict,
) -> dict:
    by_category = {}
    for cat in cat_files:
        bd = cat_breakdowns.get(cat)
        by_category[cat] = PrScoringResult(
            total_score=cat_score[cat],
            total_nodes_scored=cat_nodes[cat],
            total_lines=cat_lines[cat],
            file_results=cat_files[cat],
            score_breakdown=sum(bd, start=ScoreBreakdown()) if bd else None,
        )
    return by_category

def calculate_token_score_from_file_changes(
    file_changes: List['FileChange'],
    file_contents: Dict[str, FileContentPair],
    weights: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
) -> PrScoringResult:
    """
    Calculate contribution score using tree-sitter AST comparison.

    Args:
        file_changes: List of FileChange objects from the PR
        file_contents: Dict mapping file paths to FileContentPair(old_content, new_content)
        weights: TokenConfig instance with scoring configuration
        programming_languages: Language weight mapping (for fallback/documentation files)

    Returns:
        PrScoringResult with total score, per-file details, and per-category breakdowns
    """
    if not file_changes:
        return PrScoringResult(
            total_score=0.0,
            total_nodes_scored=0,
            total_lines=0,
            file_results=[],
        )

    file_results: List[FileScoreResult] = []

    # Per-category accumulators
    cat_files: Dict[ScoringCategory, List[FileScoreResult]] = {}
    cat_score: Dict[ScoringCategory, float] = {}
    cat_nodes: Dict[ScoringCategory, int] = {}
    cat_lines: Dict[ScoringCategory, int] = {}
    cat_breakdowns: Dict[ScoringCategory, List[ScoreBreakdown]] = {}

    total_score = 0.0
    total_nodes = 0
    total_lines = 0
    all_breakdowns: List[ScoreBreakdown] = []

    for file in file_changes:
        ext = file.file_extension or ''
        is_test_file = file.is_test_file()
        file_weight = TEST_FILE_CONTRIBUTION_WEIGHT if is_test_file else 1.0

        if file.status == 'removed':
            file_result = FileScoreResult(
                filename=file.short_name,
                score=0.0,
                nodes_scored=0,
                total_lines=file.deletions,
                is_test_file=is_test_file,
                scoring_method='skipped',
            )
        elif ext in NON_CODE_EXTENSIONS:
            file_result = _score_non_code_file(file, ext, programming_languages, file_weight, is_test_file)
        else:
            content_pair = file_contents.get(file.filename)
            file_result = _score_code_file(file, ext, content_pair, weights, programming_languages, file_weight, is_test_file)

        # Accumulate into results and per-category totals
        file_results.append(file_result)
        cat = file_result.category
        cat_files.setdefault(cat, []).append(file_result)
        cat_score[cat] = cat_score.get(cat, 0.0) + file_result.score
        cat_nodes[cat] = cat_nodes.get(cat, 0) + file_result.nodes_scored
        cat_lines[cat] = cat_lines.get(cat, 0) + file_result.total_lines
        total_score += file_result.score
        total_nodes += file_result.nodes_scored
        total_lines += file_result.total_lines
        if file_result.breakdown is not None:
            cat_breakdowns.setdefault(cat, []).append(file_result.breakdown)
            all_breakdowns.append(file_result.breakdown)

    # Build per-category sub-results
    by_category = _build_category_results(cat_files, cat_score, cat_nodes, cat_lines, cat_breakdowns)

    result = PrScoringResult(
        total_score=total_score,
        total_nodes_scored=total_nodes,
        total_lines=total_lines,
        file_results=file_results,
        score_breakdown=sum(all_breakdowns, start=ScoreBreakdown()) if all_breakdowns else None,
        by_category=by_category,
    )

    log_scoring_results(file_results, total_score, total_lines, result.score_breakdown)

    return result
