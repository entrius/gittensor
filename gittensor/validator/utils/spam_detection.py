import re
from typing import List, Optional
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

def is_comment_line(content: str, file_extension: Optional[str] = None) -> bool:
    """Check if line content is a comment using Pygments."""
    try:
        filename = f"dummy{file_extension}" if file_extension else "dummy.txt"
        lexer = get_lexer_for_filename(filename)
    except ClassNotFound:
        lexer = TextLexer()

    try:
        tokens = list(lex(content, lexer))
        
        has_content = False
        for token_type, value in tokens:
            if not value.strip():
                continue
            
            has_content = True
            if token_type not in Comment and token_type not in String.Doc:
                return False
                
        return has_content  # True if only comments/docstrings, False if empty
    except Exception:
        return False

def count_non_scoreable_lines(patch: str, max_scoreable_lines: Optional[int] = None, file_extension: Optional[str] = None) -> int:
    """Count lines that shouldn't contribute to the score (blank, comment, etc)."""
    if not patch:
        return 0
    
    non_scoreable = 0
    lines = patch.split("\n")
    scoreable_count = 0
    skip_next = False  # Track if next line should be skipped
    
    for i, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
            
        if not is_single_diff_line(line):
            continue
        
        content = line[1:]
        
        # Blank lines and comments
        if content.strip() == "" or is_comment_line(content, file_extension):
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

def is_single_diff_line(line: str) -> bool:
    """True for +foo or -bar but False for ++foo, --bar, etc."""
    if not line:
        return False
    char = line[0]
    return char in "+-" and (len(line) == 1 or line[1] != char)
