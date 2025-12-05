import re
from typing import List, Optional, Set
from Levenshtein import distance, ratio
from pygments import lex
from pygments.lexers import get_lexer_for_filename, TextLexer
from pygments.token import Comment, String
from pygments.util import ClassNotFound
from gittensor.constants import (
    TYPO_MAX_DIST,
    TYPO_MIN_SIM,
)

def tokenize(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9_'-]+", text)

def token_pair_typo(o: str, n: str, max_dist: int, min_sim: float) -> bool:
    dist = distance(o, n)
    sim = ratio(o, n)
    return dist <= max_dist or sim >= min_sim

def is_token_typo(old: str, new: str, max_dist=TYPO_MAX_DIST, min_sim=TYPO_MIN_SIM) -> bool:
    """Check if two lines are likely typo corrections of each other."""
    old_tokens = tokenize(old)
    new_tokens = tokenize(new)

    if len(old_tokens) != len(new_tokens):
        return False

    return all(token_pair_typo(o, n, max_dist, min_sim)
            for o, n in zip(old_tokens, new_tokens))

def is_single_diff_line(line: str) -> bool:
    """True for +foo or -bar but False for ++foo, --bar, etc."""
    if not line:
        return False
    char = line[0]
    return char in "+-" and (len(line) == 1 or line[1] != char)

def get_comment_line_indices(lines: List[str], file_extension: Optional[str] = None) -> Set[int]:
    """
    Analyzes all lines together to detect comments (including multi-line comments).
    
    Returns:
        A set of indices corresponding to lines that contain ONLY comments or docstrings.
    """
    if not lines:
        return set()

    # 1. Reconstruct the source code content by stripping diff markers (+, -, space)
    clean_content_parts = []
    for line in lines:
        if len(line) > 0 and line[0] in "+- ":
            clean_content_parts.append(line[1:])
        else:
            # Empty lines or diff headers are treated as empty strings to preserve index alignment
            clean_content_parts.append("")
    
    full_text = "\n".join(clean_content_parts)

    # 2. Determine the appropriate lexer
    try:
        filename = f"dummy{file_extension}" if file_extension else "dummy.txt"
        lexer = get_lexer_for_filename(filename)
    except ClassNotFound:
        lexer = TextLexer()

    # 3. Tokenize the entire text to preserve context (e.g., inside /* ... */)
    try:
        tokens = lex(full_text, lexer)
    except Exception:
        return set()

    # 4. Map tokens back to line indices
    n_lines = len(lines)
    line_has_code = [False] * n_lines
    line_has_comment = [False] * n_lines
    
    current_line_idx = 0

    for token_type, value in tokens:
        # A token might span multiple lines (e.g., block comments or multi-line strings)
        sub_lines = value.split('\n')
        
        for i, sub_line in enumerate(sub_lines):
            target_line_idx = current_line_idx + i
            
            if target_line_idx >= n_lines:
                break
                
            # If this part of the token has actual content (not just whitespace)
            if sub_line.strip():
                if token_type in Comment or token_type in String.Doc:
                    line_has_comment[target_line_idx] = True
                else:
                    # Any other token (Keyword, Name, Operator) implies code
                    line_has_code[target_line_idx] = True
        
        # Advance the line index based on the number of newlines in the current token
        current_line_idx += len(sub_lines) - 1

    # 5. Identify lines that are pure comments (has comment AND no code)
    comment_indices = set()
    for i in range(n_lines):
        if line_has_comment[i] and not line_has_code[i]:
            comment_indices.add(i)
            
    return comment_indices

def count_non_scoreable_lines(patch: str, max_scoreable_lines: Optional[int] = None, file_extension: Optional[str] = None) -> int:
    """Count lines that shouldn't contribute to the score (blank, comment, etc)."""
    if not patch:
        return 0
    
    non_scoreable = 0
    lines = patch.split("\n")
    
    # Pre-calculate comment lines using context-aware lexing
    comment_line_indices = get_comment_line_indices(lines, file_extension)

    scoreable_count = 0
    skip_next = False  # Track if next line should be skipped
    
    for i, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
            
        if not is_single_diff_line(line):
            continue
        
        content = line[1:]
        
        # Blank lines
        if content.strip() == "":
            non_scoreable += 1
            continue
        
        # Check if the current line index was identified as a comment
        if i in comment_line_indices:
            non_scoreable += 1
            continue
        
        # Typo corrections: deletion followed by similar addition
        if line.startswith("-") and i + 1 < len(lines):
            next_line = lines[i + 1]
            if is_single_diff_line(next_line) and next_line.startswith("+"):
                if is_token_typo(content, next_line[1:]):
                    non_scoreable += 2
                    skip_next = True  # Skip the + line in next iteration
                    continue
        
        # This line is scoreable
        scoreable_count += 1
        if max_scoreable_lines is not None and scoreable_count >= max_scoreable_lines:
            break

    return non_scoreable

