# The MIT License (MIT)
# Copyright © 2025 Entrius
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

import bittensor as bt
from tree_sitter import Node, Parser, Tree

from gittensor.classes import FileScoreResult, ScoreBreakdown, TokenScoringResult
from gittensor.constants import (
    MAX_FILE_SIZE_BYTES,
    MAX_LINES_SCORED_FOR_MITIGATED_EXT,
    MITIGATED_EXTENSIONS,
    TEST_FILE_CONTRIBUTION_WEIGHT,
)
from gittensor.validator.utils.load_weights import TokenWeights

if TYPE_CHECKING:
    from gittensor.classes import FileChange


# Regex to parse hunk headers: @@ -old_start,old_count +new_start,new_count @@
HUNK_HEADER_PATTERN = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')

# Simple tokenizer pattern: split on whitespace and common delimiters, keep tokens
TOKEN_PATTERN = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]*|[0-9]+(?:\.[0-9]+)?|"[^"]*"|\'[^\']*\'|[^\s]')

# Cache parsers to avoid repeated initialization
_parser_cache: Dict[str, Parser] = {}


@dataclass
class LineChangeInfo:
    """Info about what changed on a specific line."""

    line_num: int
    is_pure_addition: bool
    changed_tokens: Set[str]  # Tokens that were added/changed (empty for pure additions)


def extract_added_lines(patch: Optional[str]) -> Set[int]:
    """
    Extract line numbers of added lines from a unified diff patch.

    Parses the patch to find all lines that start with '+' (excluding hunk headers)
    and returns their 1-based line numbers in the NEW file.

    Args:
        patch: Unified diff patch string (as returned by GitHub API)

    Returns:
        Set of 1-based line numbers that were added

    Example:
        patch = '''@@ -10,3 +10,5 @@
         context
        -deleted
        +added line 1
        +added line 2
         context'''

        extract_added_lines(patch) -> {11, 12}
    """
    if not patch:
        return set()

    added_lines: Set[int] = set()
    current_line = 0  # Will be set by hunk header

    for line in patch.split('\n'):
        # Check for hunk header
        match = HUNK_HEADER_PATTERN.match(line)
        if match:
            # Extract starting line number in the new file
            current_line = int(match.group(1))
            continue

        # Skip if we haven't seen a hunk header yet
        if current_line == 0:
            continue

        # Determine line type by first character
        if not line:
            continue

        first_char = line[0]

        if first_char == '+':
            # Added line - record it and increment
            added_lines.add(current_line)
            current_line += 1
        elif first_char == '-':
            # Deleted line - don't increment (not in new file)
            pass
        elif first_char == ' ':
            # Context line - increment
            current_line += 1
        elif first_char == '\\':
            # "\ No newline at end of file" - ignore
            pass
        else:
            # Unknown line type, treat as context
            current_line += 1

    return added_lines


def tokenize_line(line: str) -> List[str]:
    """
    Tokenize a line of code into meaningful tokens.

    Returns list of tokens (identifiers, numbers, strings, operators).
    """
    return TOKEN_PATTERN.findall(line)


def normalize_token(token: str) -> str:
    """Normalize a token for comparison (strip quotes, whitespace)."""
    return token.strip('"\'`').strip()


def get_changed_tokens(old_line: str, new_line: str) -> Set[str]:
    """
    Get tokens that are in new_line but not in old_line.

    Returns:
        Set of normalized tokens that were added/changed
    """
    old_tokens = set(normalize_token(t) for t in tokenize_line(old_line))
    new_tokens = set(normalize_token(t) for t in tokenize_line(new_line))

    # Tokens in new but not in old
    delta = new_tokens - old_tokens

    # Filter out empty strings and pure whitespace
    return {t for t in delta if t and not t.isspace()}


def extract_patch_changes(patch: Optional[str]) -> Dict[int, LineChangeInfo]:
    """
    Parse patch to categorize each added line as pure addition or modification.

    For modifications (- followed by +), extracts the tokens that changed.
    For pure additions (+ without preceding -), marks as pure addition.

    Args:
        patch: Unified diff patch string

    Returns:
        Dict mapping new file line numbers to LineChangeInfo

    Example:
        patch = '''@@ -1,3 +1,4 @@
         def foo():
        -    x = 1
        +    x = 2
        +    y = 3'''

        Result:
        {
            2: LineChangeInfo(line_num=2, is_pure_addition=False, changed_tokens={'2'}),
            3: LineChangeInfo(line_num=3, is_pure_addition=True, changed_tokens=set()),
        }
    """
    if not patch:
        return {}

    changes: Dict[int, LineChangeInfo] = {}
    current_new_line = 0
    pending_deletions: List[str] = []  # Buffer for - lines waiting for potential + pairs

    lines = patch.split('\n')
    i = 0

    while i < len(lines):
        line = lines[i]

        # Check for hunk header
        match = HUNK_HEADER_PATTERN.match(line)
        if match:
            current_new_line = int(match.group(1))
            pending_deletions = []  # Reset on new hunk
            i += 1
            continue

        # Skip if we haven't seen a hunk header yet
        if current_new_line == 0:
            i += 1
            continue

        if not line:
            i += 1
            continue

        first_char = line[0]

        if first_char == '-':
            # Deletion - buffer it for potential pairing with addition
            pending_deletions.append(line[1:])  # Store content without the -
            i += 1

        elif first_char == '+':
            new_content = line[1:]  # Content without the +

            if pending_deletions:
                # This is a modification - pair with first pending deletion
                old_content = pending_deletions.pop(0)
                changed_tokens = get_changed_tokens(old_content, new_content)

                changes[current_new_line] = LineChangeInfo(
                    line_num=current_new_line,
                    is_pure_addition=False,
                    changed_tokens=changed_tokens,
                )
            else:
                # Pure addition - no preceding deletion
                changes[current_new_line] = LineChangeInfo(
                    line_num=current_new_line,
                    is_pure_addition=True,
                    changed_tokens=set(),
                )

            current_new_line += 1
            i += 1

        elif first_char == ' ':
            # Context line - clear pending deletions (they weren't paired)
            pending_deletions = []
            current_new_line += 1
            i += 1

        elif first_char == '\\':
            # "\ No newline at end of file" - ignore
            i += 1

        else:
            # Unknown line type
            pending_deletions = []
            current_new_line += 1
            i += 1

    return changes


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


def calculate_line_scores(
    content: str,
    extension: str,
    weights: TokenWeights,
) -> Dict[int, float]:
    """
    Calculate per-line scores using tree-sitter AST analysis.

    Args:
        content: Full file content as string
        extension: File extension (with or without dot)
        weights: TokenWeights instance with scoring configuration

    Returns:
        Dict mapping 1-based line numbers to scores.
        Empty dict if parsing fails or language not supported.
    """
    language = weights.get_language(extension)
    if not language:
        return {}

    tree = parse_code(content, language)
    if not tree:
        return {}

    line_scores: Dict[int, float] = {}

    def add_score(line: int, score: float) -> None:
        """Add score to a line (1-based)."""
        if score > 0:
            line_scores[line] = line_scores.get(line, 0.0) + score

    def walk_node(node: Node) -> None:
        """Recursively walk the AST and accumulate scores."""
        node_type = node.type
        # Tree-sitter uses 0-based lines, convert to 1-based
        start_line = node.start_point[0] + 1

        # Check for structural bonus (applied to start line)
        structural_weight = weights.get_structural_weight(node_type)
        if structural_weight > 0:
            add_score(start_line, structural_weight)

        # Check for leaf token weight
        if node.child_count == 0:
            leaf_weight = weights.get_leaf_weight(node_type)
            add_score(start_line, leaf_weight)

        # Recurse into children
        for child in node.children:
            walk_node(child)

    walk_node(tree.root_node)
    return line_scores


def calculate_total_score(
    content: str,
    extension: str,
    weights: TokenWeights,
    added_lines: Optional[set[int]] = None,
) -> float:
    """
    Calculate total score for a file, optionally filtered by specific lines.

    Args:
        content: Full file content as string
        extension: File extension (with or without dot)
        weights: TokenWeights instance
        added_lines: Optional set of 1-based line numbers to score.
                     If None, scores all lines.

    Returns:
        Total score as float
    """
    line_scores = calculate_line_scores(content, extension, weights)

    if added_lines is None:
        return sum(line_scores.values())

    return sum(score for line, score in line_scores.items() if line in added_lines)


def calculate_line_scores_with_changes(
    content: str,
    extension: str,
    weights: TokenWeights,
    change_info: Dict[int, LineChangeInfo],
) -> Dict[int, float]:
    """
    Calculate per-line scores with change-aware logic.

    For pure additions: score the full line (structural + leaf weights)
    For modifications: only score leaf tokens that match changed_tokens

    This prevents gaming by making trivial changes to high-value lines.

    Args:
        content: Full file content as string
        extension: File extension (with or without dot)
        weights: TokenWeights instance with scoring configuration
        change_info: Dict mapping line numbers to LineChangeInfo

    Returns:
        Dict mapping 1-based line numbers to scores.
        Only lines in change_info are scored.
    """
    language = weights.get_language(extension)
    if not language:
        return {}

    tree = parse_code(content, language)
    if not tree:
        return {}

    line_scores: Dict[int, float] = {}

    def add_score(line: int, score: float) -> None:
        """Add score to a line (1-based)."""
        if score > 0:
            line_scores[line] = line_scores.get(line, 0.0) + score

    def node_matches_changed_token(node: Node, changed_tokens: Set[str]) -> bool:
        """
        Check if the node's text matches any changed token.

        Matching rules:
        - For short tokens (<=2 chars): require exact match to avoid false positives
          (e.g., 'c' shouldn't match 'calculate')
        - For longer tokens: allow substring match to handle string content changes
          (e.g., 'world' should match '"hello world"')
        """
        if not changed_tokens:
            return False
        node_text = node.text.decode('utf-8')
        normalized_node_text = normalize_token(node_text)

        for tok in changed_tokens:
            if len(tok) <= 2:
                # Short token: exact match only
                if tok == normalized_node_text:
                    return True
            else:
                # Longer token: substring match OK
                if tok in normalized_node_text:
                    return True
        return False

    def walk_node(node: Node) -> None:
        """Recursively walk the AST with change-aware scoring."""
        node_type = node.type
        start_line = node.start_point[0] + 1  # Convert to 1-based

        # Only process lines we have change info for
        if start_line not in change_info:
            # Still recurse - children might be on different lines
            for child in node.children:
                walk_node(child)
            return

        info = change_info[start_line]

        # Skip comments entirely (score 0)
        if node_type in ('comment', 'line_comment', 'block_comment', 'documentation_comment'):
            return

        if info.is_pure_addition:
            # Pure addition: full line scoring (structural + leaf)
            structural_weight = weights.get_structural_weight(node_type)
            if structural_weight > 0:
                add_score(start_line, structural_weight)

            if node.child_count == 0:
                leaf_weight = weights.get_leaf_weight(node_type)
                add_score(start_line, leaf_weight)

        else:
            # Modification: only score leaf tokens that match changed tokens
            # No structural bonus (structure already existed)
            if node.child_count == 0:
                if node_matches_changed_token(node, info.changed_tokens):
                    leaf_weight = weights.get_leaf_weight(node_type)
                    add_score(start_line, leaf_weight)

        # Recurse into children
        for child in node.children:
            walk_node(child)

    walk_node(tree.root_node)
    return line_scores


def calculate_total_score_with_changes(
    content: str,
    extension: str,
    weights: TokenWeights,
    patch: str,
) -> float:
    """
    Calculate total score for a file using change-aware scoring.

    This is the main entry point for the new token-based scoring that:
    - Gives full line scores for pure additions
    - Only scores changed tokens for modifications

    Args:
        content: Full file content as string
        extension: File extension (with or without dot)
        weights: TokenWeights instance
        patch: The unified diff patch for this file

    Returns:
        Total score as float
    """
    breakdown = calculate_score_with_breakdown(content, extension, weights, patch)
    return breakdown.total_score


def calculate_score_with_breakdown(
    content: str,
    extension: str,
    weights: TokenWeights,
    patch: str,
) -> ScoreBreakdown:
    """
    Calculate score with detailed breakdown of structural vs leaf contributions.

    Args:
        content: Full file content as string
        extension: File extension (with or without dot)
        weights: TokenWeights instance
        patch: The unified diff patch for this file

    Returns:
        ScoreBreakdown with total score and structural/leaf breakdown
    """
    change_info = extract_patch_changes(patch)
    if not change_info:
        return ScoreBreakdown()

    language = weights.get_language(extension)
    if not language:
        return ScoreBreakdown()

    tree = parse_code(content, language)
    if not tree:
        return ScoreBreakdown()

    # Track breakdown
    breakdown = ScoreBreakdown()

    def node_matches_changed_token(node: Node, changed_tokens: Set[str]) -> bool:
        """Check if node matches any changed token."""
        if not changed_tokens:
            return False
        node_text = node.text.decode('utf-8')
        normalized_node_text = normalize_token(node_text)

        for tok in changed_tokens:
            if len(tok) <= 2:
                if tok == normalized_node_text:
                    return True
            else:
                if tok in normalized_node_text:
                    return True
        return False

    def walk_node(node: Node) -> None:
        """Recursively walk the AST with change-aware scoring and breakdown tracking."""
        nonlocal breakdown
        node_type = node.type
        start_line = node.start_point[0] + 1  # Convert to 1-based

        # Only process lines we have change info for
        if start_line not in change_info:
            for child in node.children:
                walk_node(child)
            return

        info = change_info[start_line]

        # Skip comments entirely
        if node_type in ('comment', 'line_comment', 'block_comment', 'documentation_comment'):
            return

        if info.is_pure_addition:
            # Pure addition: full line scoring (structural + leaf)
            structural_weight = weights.get_structural_weight(node_type)
            if structural_weight > 0:
                breakdown.structural_count += 1
                breakdown.structural_score += structural_weight
                breakdown.total_score += structural_weight

            if node.child_count == 0:
                leaf_weight = weights.get_leaf_weight(node_type)
                if leaf_weight > 0:
                    breakdown.leaf_count += 1
                    breakdown.leaf_score += leaf_weight
                    breakdown.total_score += leaf_weight

        else:
            # Modification: only score leaf tokens that match changed tokens
            if node.child_count == 0:
                if node_matches_changed_token(node, info.changed_tokens):
                    leaf_weight = weights.get_leaf_weight(node_type)
                    if leaf_weight > 0:
                        breakdown.leaf_count += 1
                        breakdown.leaf_score += leaf_weight
                        breakdown.total_score += leaf_weight

        # Recurse into children
        for child in node.children:
            walk_node(child)

    walk_node(tree.root_node)
    return breakdown


def calculate_token_score_from_file_changes(
    file_changes: List['FileChange'],
    file_contents: Dict[str, Optional[str]],
    weights: TokenWeights,
    programming_languages: Dict[str, float],
) -> TokenScoringResult:
    """
    Calculate contribution score using tree-sitter token-based analysis.

    This is a parallel implementation to the existing line-based scoring,
    using AST analysis for more accurate scoring that:
    - Properly handles multiline strings/docstrings (content inside scores low/zero)
    - Gives structural bonuses for function/class definitions
    - Weights different token types appropriately

    Args:
        file_changes: List of FileChange objects from the PR
        file_contents: Dict mapping file paths to their full content (from GraphQL fetch)
        weights: TokenWeights instance with scoring configuration
        programming_languages: Language weight mapping (for fallback/documentation files)

    Returns:
        TokenScoringResult with total score, low-value flag, and per-file details
    """
    if not file_changes:
        return TokenScoringResult(
            total_score=0.0,
            is_low_value_pr=True,
            total_lines_scored=0,
            file_results=[],
        )

    file_results: List[FileScoreResult] = []
    total_score = 0.0
    total_lines_scored = 0
    total_raw_lines = 0
    substantive_lines = 0

    # Aggregate breakdown across all files
    total_structural_count = 0
    total_structural_score = 0.0
    total_leaf_count = 0
    total_leaf_score = 0.0

    for file in file_changes:
        ext = file.file_extension
        is_test_file = file.is_test_file()
        test_weight = TEST_FILE_CONTRIBUTION_WEIGHT if is_test_file else 1.0

        # Skip deleted files (status == 'removed')
        if file.status == 'removed':
            file_results.append(
                FileScoreResult(
                    filename=file.short_name,
                    score=0.0,
                    lines_scored=0,
                    total_lines=file.deletions,
                    is_test_file=is_test_file,
                    scoring_method='skipped',
                )
            )
            continue

        # Get file content
        content = file_contents.get(file.filename)

        # Handle missing/binary files - score 0
        if content is None:
            bt.logging.debug(f'  │   {file.short_name}: skipped (binary or fetch failed)')
            file_results.append(
                FileScoreResult(
                    filename=file.short_name,
                    score=0.0,
                    lines_scored=0,
                    total_lines=file.additions,
                    is_test_file=is_test_file,
                    scoring_method='skipped-binary',
                )
            )
            continue

        # Check file size - score 0 for large files
        if len(content.encode('utf-8')) > MAX_FILE_SIZE_BYTES:
            bt.logging.debug(f'  │   {file.short_name}: skipped (file too large, >{MAX_FILE_SIZE_BYTES} bytes)')
            file_results.append(
                FileScoreResult(
                    filename=file.short_name,
                    score=0.0,
                    lines_scored=0,
                    total_lines=file.additions,
                    is_test_file=is_test_file,
                    scoring_method='skipped-large',
                )
            )
            continue

        # Extract added lines from patch
        added_lines = extract_added_lines(file.patch)

        # Initialize breakdown for this file
        file_structural_count = 0
        file_structural_score = 0.0
        file_leaf_count = 0
        file_leaf_score = 0.0

        # Determine scoring method based on extension
        if weights.supports_tree_sitter(ext):
            # Use tree-sitter AST scoring with change-aware logic and breakdown
            breakdown = calculate_score_with_breakdown(content, ext, weights, file.patch)
            file_score = breakdown.total_score
            file_structural_count = breakdown.structural_count
            file_structural_score = breakdown.structural_score
            file_leaf_count = breakdown.leaf_count
            file_leaf_score = breakdown.leaf_score
            lines_scored = len(added_lines)
            scoring_method = 'tree-sitter'

        elif ext in MITIGATED_EXTENSIONS:
            # Documentation/config files - use capped line count
            lines_to_score = min(len(added_lines), MAX_LINES_SCORED_FOR_MITIGATED_EXT)
            lang_weight = programming_languages.get(ext, 0.1)
            file_score = lang_weight * lines_to_score
            lines_scored = lines_to_score
            scoring_method = 'line-count'

        else:
            # Unknown extension not in tree-sitter or mitigated - score 0
            bt.logging.debug(f'  │   {file.short_name}: skipped (extension .{ext} not supported)')
            file_results.append(
                FileScoreResult(
                    filename=file.short_name,
                    score=0.0,
                    lines_scored=0,
                    total_lines=file.additions,
                    is_test_file=is_test_file,
                    scoring_method='skipped-unsupported',
                )
            )
            continue

        # Apply test file weight
        file_score *= test_weight
        file_structural_score *= test_weight
        file_leaf_score *= test_weight

        # Track substantive changes (non-test, non-mitigated)
        is_substantive = not is_test_file and ext not in MITIGATED_EXTENSIONS
        if is_substantive:
            substantive_lines += lines_scored

        total_score += file_score
        total_lines_scored += lines_scored
        total_raw_lines += len(added_lines)

        # Aggregate breakdown
        total_structural_count += file_structural_count
        total_structural_score += file_structural_score
        total_leaf_count += file_leaf_count
        total_leaf_score += file_leaf_score

        file_results.append(
            FileScoreResult(
                filename=file.short_name,
                score=file_score,
                lines_scored=lines_scored,
                total_lines=len(added_lines),
                is_test_file=is_test_file,
                scoring_method=scoring_method,
                structural_count=file_structural_count,
                structural_score=file_structural_score,
                leaf_count=file_leaf_count,
                leaf_score=file_leaf_score,
            )
        )

    # Determine if this is a low-value PR (>90% non-substantive)
    substantive_ratio = substantive_lines / total_raw_lines if total_raw_lines > 0 else 0
    is_low_value_pr = substantive_ratio < 0.1

    # Log results
    _log_scoring_results(file_results, total_score, substantive_lines, total_raw_lines)

    return TokenScoringResult(
        total_score=total_score,
        is_low_value_pr=is_low_value_pr,
        total_lines_scored=total_lines_scored,
        file_results=file_results,
        total_structural_count=total_structural_count,
        total_structural_score=total_structural_score,
        total_leaf_count=total_leaf_count,
        total_leaf_score=total_leaf_score,
    )


def _score_file_fallback(
    file: 'FileChange',
    programming_languages: Dict[str, float],
) -> Tuple[float, int]:
    """
    Fallback scoring for files we can't parse (binary, too large, etc.).

    Uses simple line count * language weight.

    Returns:
        Tuple of (score, lines_scored)
    """
    lang_weight = programming_languages.get(file.file_extension, 0.1)
    lines = file.additions
    return lang_weight * lines, lines


def _log_scoring_results(
    file_results: List[FileScoreResult],
    total_score: float,
    substantive_lines: int,
    total_raw_lines: int,
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
                f'{result.lines_scored:>3}/{result.total_lines:<3} lines  '
                f'{result.score:>6.2f}{test_mark}{method_mark}'
            )

    substantive_pct = (substantive_lines / total_raw_lines * 100) if total_raw_lines > 0 else 0
    bt.logging.info(
        f'  ├─ Token Score: {total_score:.2f} | '
        f'Substantive: {substantive_lines}/{total_raw_lines} ({substantive_pct:.0f}%)'
    )
