from typing import List, Tuple
import re

from gittensor.constants import (
    TYPO_ONLY_PR_PENALTY,
    MIN_TYPO_RATIO_THRESHOLD,
    TYPO_KEYWORDS,
    TRANSLATION_ONLY_PR_PENALTY,
    MIN_TRANSLATION_RATIO_THRESHOLD,
    TRANSLATION_FILE_PATTERNS,
    TRANSLATION_KEYWORDS,
    TRANSLATION_CONTENT_PATTERNS,
    NON_ENGLISH_UNICODE_RANGES
)

def is_typo_change(patch: str) -> bool:
    """
    Detect if a patch contains only typo/formatting changes.
    
    Checks for:
    - Single character changes
    - Whitespace-only changes
    - Punctuation changes
    - Common typo patterns
    
    Args:
        patch: The git diff patch string
        
    Returns:
        bool: True if the change appears to be a typo fix
    """
    if not patch:
        return False
    
    # Split patch into lines
    lines = patch.split('\n')
    
    # Count meaningful changes vs typo changes
    typo_changes = 0
    total_changes = 0
    
    for line in lines:
        # Skip diff metadata lines
        if line.startswith('@@') or line.startswith('+++') or line.startswith('---'):
            continue
        
        # Only look at added/removed lines
        if not (line.startswith('+') or line.startswith('-')):
            continue
        
        # Remove the +/- prefix
        content = line[1:]
        
        # Skip empty lines
        if not content.strip():
            continue
        
        total_changes += 1
        
        # Check for typo patterns
        # 1. Single character difference (likely typo)
        if len(content.strip()) <= 2:
            typo_changes += 1
            continue
        
        # 2. Only whitespace changes
        if content != content.strip() and content.strip() == '':
            typo_changes += 1
            continue
        
        # 3. Only punctuation changes
        if re.match(r'^[\s\.,;:!?\'"]+$', content):
            typo_changes += 1
            continue
        
        # 4. Very small word changes (1-3 chars different)
        # This is a heuristic - if line is short and simple, likely a typo
        if len(content.strip()) < 20 and not re.search(r'[{}\[\]()<>=]', content):
            typo_changes += 1
    
    # If we have changes and most are typos, flag it
    if total_changes > 0:
        typo_ratio = typo_changes / total_changes
        return typo_ratio >= 0.6  # 60% or more are typo-like changes
    
    return False


def is_translation_file(filename: str) -> bool:
    """
    Check if a file is a translation/localization file.
    
    Args:
        filename: The file path
        
    Returns:
        bool: True if file is a translation file
    """
    filename_lower = filename.lower()
    
    # Check for translation file patterns
    for pattern in TRANSLATION_FILE_PATTERNS:
        if pattern in filename_lower:
            return True
    
    return False


def is_translation_content(patch: str) -> bool:
    """Detect if patch content resembles translation data or AI-translated text."""
    
    if not patch:
        return False

    lines = patch.splitlines()
    if not lines:
        return False

    total_lines = 0
    translation_like_lines = 0
    foreign_chars = 0

    for line in lines:
        if not (line.startswith('+') or line.startswith('-')):
            continue
        content = line[1:].strip()
        if not content:
            continue

        total_lines += 1

        # Check for translation content patterns (key-value pairs, XML, etc.)
        if any(re.search(pat, content) for pat in TRANSLATION_CONTENT_PATTERNS):
            translation_like_lines += 1
            continue

        # Detect significant non-English text (based on unicode ranges)
        for start, end in NON_ENGLISH_UNICODE_RANGES:
            if any(start <= ord(ch) <= end for ch in content):
                foreign_chars += 1
                break

    if total_lines == 0:
        return False

    # Compute ratios
    translation_ratio = max(translation_like_lines, foreign_chars) / total_lines
    
    return translation_ratio >= 0.5  # 50%+ translation-like lines


def extract_word_changes_from_patch(patch: str) -> List[Tuple[str, str]]:
    """
    Extract word-level changes from a git patch.
    
    Analyzes added/removed line pairs to find what words changed.
    
    Args:
        patch: Git diff patch string
        
    Returns:
        List of (old_word, new_word) tuples representing changes
    """
    if not patch:
        return []
    
    lines = patch.split('\n')
    word_changes = []
    
    # Group consecutive removed and added lines
    removed_lines = []
    added_lines = []
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Collect consecutive removed lines
        if line.startswith('-') and not line.startswith('---'):
            removed_lines.append(line[1:])  # Remove '-' prefix
            i += 1
            continue
        
        # Collect consecutive added lines
        if line.startswith('+') and not line.startswith('+++'):
            added_lines.append(line[1:])  # Remove '+' prefix
            i += 1
            continue
        
        # When we hit a non-change line, process accumulated changes
        if removed_lines and added_lines:
            # Compare removed vs added lines to find word changes
            for removed, added in zip(removed_lines, added_lines):
                changes = find_word_differences(removed, added)
                word_changes.extend(changes)
            
            removed_lines = []
            added_lines = []
        
        i += 1
    
    # Process any remaining changes
    if removed_lines and added_lines:
        for removed, added in zip(removed_lines, added_lines):
            changes = find_word_differences(removed, added)
            word_changes.extend(changes)
    
    return word_changes


def find_word_differences(old_line: str, new_line: str) -> List[Tuple[str, str]]:
    """
    Find word-level differences between two lines.
    
    Args:
        old_line: Original line
        new_line: Modified line
        
    Returns:
        List of (old_word, new_word) tuples
    """
    # Tokenize into words (alphanumeric sequences)
    old_words = re.findall(r'\b\w+\b', old_line)
    new_words = re.findall(r'\b\w+\b', new_line)
    
    # If lines are very different in length, skip (likely not a simple typo)
    if abs(len(old_words) - len(new_words)) > 2:
        return []
    
    word_changes = []
    
    # Simple word-by-word comparison
    for i, (old_word, new_word) in enumerate(zip(old_words, new_words)):
        if old_word != new_word:
            # Only consider it a change if words are similar (likely typo or rename)
            if is_similar_word(old_word, new_word):
                word_changes.append((old_word.lower(), new_word.lower()))
    
    return word_changes


def is_similar_word(word1: str, word2: str) -> bool:
    """
    Check if two words are similar (likely typo or intentional rename).
    
    Criteria:
    - Similar length (within 3 chars)
    - Share common prefix/suffix
    - Levenshtein distance is small
    
    Args:
        word1: First word
        word2: Second word
        
    Returns:
        bool: True if words are similar
    """
    # Must be somewhat similar in length
    if abs(len(word1) - len(word2)) > 3:
        return False
    
    # Very short words - must be exact or 1 char different
    if len(word1) <= 3 or len(word2) <= 3:
        return levenshtein_distance(word1, word2) <= 1
    
    # Longer words - allow more difference but check for common parts
    distance = levenshtein_distance(word1, word2)
    max_distance = max(len(word1), len(word2)) // 3  # Allow 33% difference
    
    return distance <= max_distance


def levenshtein_distance(s1: str, s2: str) -> int:
    """
    Calculate Levenshtein distance between two strings.
    
    Args:
        s1: First string
        s2: Second string
        
    Returns:
        int: Edit distance
    """
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    
    if len(s2) == 0:
        return len(s1)
    
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            # Cost of insertions, deletions, or substitutions
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    
    return previous_row[-1]


def normalize_code_tokens(code: str) -> List[str]:
    """
    Normalize code into semantic tokens, ignoring formatting.
    Works for JS/TS/Python/Go/Java/etc.
    """
    # Remove whitespace changes
    code = re.sub(r'\s+', ' ', code)

    # Remove punctuation-only formatting artifacts
    code = code.replace(";", "")
    
    # Remove standalone braces or brace positioning changes
    code = code.replace("{", "").replace("}", "")
    code = code.replace("(", " ( ").replace(")", " ) ")
    code = re.sub(r'\s+', ' ', code)

    # Tokenize into meaningful identifiers/keywords
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+|==|!=|<=|>=|=>|=|\+|-|\*|/", code)


def is_formatting_change(old_code: str, new_code: str) -> bool:
    old_tokens = normalize_code_tokens(old_code)
    new_tokens = normalize_code_tokens(new_code)
    return old_tokens == new_tokens


def looks_like_whitespace_formatting(line: str) -> bool:
    content = line.strip()

    # Added indentation
    if re.match(r'^[{}()\[\];,\s]*$', content):
        return True

    # Change in spacing between tokens, like:
    # "if(x)" → "if ( x )"
    if re.match(r'^[A-Za-z0-9_\s{}();,\[\]]+$', content):
        return True

    return False