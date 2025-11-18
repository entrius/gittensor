# The MIT License (MIT)
# Copyright © 2025 Entrius

import re

from gittensor.classes import PullRequest
from gittensor.constants import TYPO_DETECTION_CONFIDENCE_THRESHOLD


def is_typo_only_pr(pr: PullRequest) -> bool:
    """
    Determines if a PR is typo-only with >=85% confidence.
    
    Analyzes file changes and patch content to detect typo-fix patterns:
    - Single word replacements
    - Small character changes in words
    - Changes primarily in text/documentation files
    - Minimal code changes
    
    Args:
        pr (PullRequest): The pull request to analyze
        
    Returns:
        bool: True if confidence >= threshold that PR is typo-only
    """
    if not pr.file_changes:
        return False
    
    typo_indicators = 0
    total_indicators = 0
    
    text_file_extensions = {'md', 'txt', 'rst', 'adoc', 'org'}
    code_comment_pattern = re.compile(r'^\s*[#*/]', re.MULTILINE)
    
    for file_change in pr.file_changes:
        ext = file_change.file_extension
        patch = file_change.patch or ""
        changes = file_change.changes
        
        # Skip binary or very large changes
        if changes > 50 or not patch:
            total_indicators += 1
            continue
        
        # Check if file is primarily text/documentation
        is_text_file = ext in text_file_extensions
        if is_text_file:
            typo_indicators += 1
        total_indicators += 1
        
        # Analyze patch for typo patterns
        typo_patterns = _analyze_patch_for_typos(patch)
        if typo_patterns > 0:
            typo_indicators += min(typo_patterns, 2)  # Cap contribution per file
        total_indicators += 1
        
        # Check if changes are in comments
        comment_changes = _count_comment_changes(patch, code_comment_pattern)
        if comment_changes > 0 and comment_changes / max(changes, 1) > 0.7:
            typo_indicators += 1
        total_indicators += 1
    
    if total_indicators == 0:
        return False
    
    confidence = typo_indicators / total_indicators
    return confidence >= TYPO_DETECTION_CONFIDENCE_THRESHOLD


def _analyze_patch_for_typos(patch: str) -> int:
    """
    Analyzes patch content for typo-fix patterns.
    
    Returns count of typo-like patterns found.
    """
    if not patch:
        return 0
    
    typo_count = 0
    lines = patch.split('\n')
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Look for consecutive -/+ lines (typical diff format)
        if line.startswith('-') and i + 1 < len(lines) and lines[i + 1].startswith('+'):
            old_content = line[1:].strip()
            new_content = lines[i + 1][1:].strip()
            
            # Single word replacements
            if _is_single_word_change(old_content, new_content):
                typo_count += 1
            # Single character differences in words
            elif _is_single_char_diff_in_text(old_content, new_content):
                typo_count += 1
            
            i += 2
        else:
            i += 1
    
    return min(typo_count, 5)  # Cap to prevent over-weighting


def _is_similar_length(s1: str, s2: str) -> bool:
    """Check if two strings have similar length (within 2 chars)."""
    return abs(len(s1) - len(s2)) <= 2


def _is_single_word_change(old: str, new: str) -> bool:
    """Check if change is a single word replacement (typo pattern)."""
    old_words = old.split()
    new_words = new.split()
    if len(old_words) == 1 and len(new_words) == 1:
        old_word = old_words[0].strip('.,!?;:')
        new_word = new_words[0].strip('.,!?;:')
        if len(old_word) > 2 and len(new_word) > 2 and _is_similar_length(old_word, new_word):
            return True
    return False


def _is_single_char_diff_in_text(old: str, new: str) -> bool:
    """Check if text differs by single character changes (typo pattern)."""
    if abs(len(old) - len(new)) > 2:
        return False
    # Extract words and check for single char differences
    old_words = re.findall(r'\b\w+\b', old)
    new_words = re.findall(r'\b\w+\b', new)
    if len(old_words) == len(new_words) and len(old_words) <= 3:
        matches = sum(1 for o, n in zip(old_words, new_words) if _is_single_char_diff(o, n))
        return matches > 0 and matches == len(old_words)
    return False


def _is_single_char_diff(s1: str, s2: str) -> bool:
    """Check if strings differ by exactly one character (common typo pattern)."""
    if abs(len(s1) - len(s2)) > 1:
        return False
    if len(s1) == len(s2):
        diffs = sum(c1 != c2 for c1, c2 in zip(s1, s2))
        return diffs == 1
    return False


def _count_comment_changes(patch: str, comment_pattern: re.Pattern) -> int:
    """Count changes that occur in comment lines."""
    if not patch:
        return 0
    
    lines = patch.split('\n')
    comment_changes = 0
    
    for line in lines:
        if line.startswith('-') or line.startswith('+'):
            content = line[1:].strip()
            if comment_pattern.match(content):
                comment_changes += 1
    
    return comment_changes

