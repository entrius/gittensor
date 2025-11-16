# The MIT License (MIT)
# Copyright © 2025 Entrius
# Contributor: James4u aka SmartDever02

"""
Spam Detection Utilities

Detects and penalizes low-quality PRs:
- Space-only PRs (format/prettier fixes)
- Typo-only PRs (minor spelling/grammar fixes)
- Comment-only PRs (no actual code changes)
- Formatting-only PRs (linting/formating no actual code changes)
- Translation-only PRs (AI-generated translations)
"""

import re
from typing import List, Tuple

import bittensor as bt

from gittensor.classes import PullRequest, FileChange
from gittensor.constants import (
    TYPO_ONLY_PR_PENALTY,
    WHITESPACE_ONLY_PR_PENALTY,
    COMMENT_ONLY_PR_PENALTY,
    FORMATTING_ONLY_PR_PENALTY,
    MIN_TYPO_RATIO_THRESHOLD,
    TYPO_KEYWORDS,
    FORMATTING_KEYWORDS,
    TRANSLATION_ONLY_PR_PENALTY,
    MIN_TRANSLATION_RATIO_THRESHOLD,
    TRANSLATION_FILE_PATTERNS,
    TRANSLATION_KEYWORDS,
    TRANSLATION_CONTENT_PATTERNS,
    NON_ENGLISH_UNICODE_RANGES
)
from gittensor.validator.utils.spam_detection import (
    is_typo_change,
    is_translation_file,
    is_translation_content,
    extract_word_changes_from_patch,
    find_word_differences,
    is_similar_word,
    levenshtein_distance,
    is_formatting_change,
    looks_like_whitespace_formatting
)
from gittensor.utils.utils import mask_secret


def analyze_repeated_changes(word_changes: List[Tuple[str, str]]) -> Tuple[int, int, List[Tuple[str, str, int]]]:
    """
    Analyze word changes to find repeated patterns.
    
    Args:
        word_changes: List of (old_word, new_word) tuples
        
    Returns:
        Tuple of:
        - total_changes: Total number of word changes
        - unique_changes: Number of unique change patterns
        - repeated_patterns: List of (old, new, count) for patterns that repeat
    """
    from collections import Counter
    
    if not word_changes:
        return 0, 0, []
    
    # Count occurrences of each change pattern
    change_counter = Counter(word_changes)
    
    total_changes = len(word_changes)
    unique_changes = len(change_counter)
    
    # Find patterns that repeat (appear 3+ times)
    repeated_patterns = [
        (old, new, count)
        for (old, new), count in change_counter.most_common()
        if count >= 3
    ]
    
    return total_changes, unique_changes, repeated_patterns


def detect_typo_only_pr(pr: PullRequest) -> Tuple[bool, float]:
    """
    Detect if a PR contains only typo/formatting fixes using smart pattern analysis.
    
    Checks:
    1. PR title/description contains typo keywords
    2. Repeated word changes across multiple lines (indicates typo fix or variable rename)
    3. Small, similar word changes (typical of typos)
    
    Args:
        pr: PullRequest object with file_changes
        
    Returns:
        Tuple[bool, float]: (is_typo_only, typo_confidence_score)
    """
    if not pr.file_changes:
        return False, 0.0
    
    # Check 1: Title/description contains typo keywords
    title_lower = pr.title.lower()
    desc_lower = (pr.description or '').lower()
    
    has_typo_keyword = any(
        keyword in title_lower or keyword in desc_lower
        for keyword in TYPO_KEYWORDS
    )
    
    # Check 2: Analyze all word changes across all files
    all_word_changes = []
    total_files_analyzed = 0
    
    for file_change in pr.file_changes:
        if not file_change.patch:
            continue
        
        total_files_analyzed += 1
        word_changes = extract_word_changes_from_patch(file_change.patch)
        all_word_changes.extend(word_changes)
    
    if not all_word_changes:
        # No word changes detected, check if it's just whitespace/formatting
        is_formatting_only = all(
            is_typo_change(fc.patch) for fc in pr.file_changes if fc.patch
        )
        return is_formatting_only, 1.0 if is_formatting_only else 0.0
    
    # Check 3: Analyze patterns in word changes
    total_changes, unique_changes, repeated_patterns = analyze_repeated_changes(all_word_changes)
    
    # Calculate metrics
    repetition_ratio = 1.0 - (unique_changes / total_changes) if total_changes > 0 else 0.0
    has_repeated_patterns = len(repeated_patterns) > 0
    
    # Log analysis for debugging
    bt.logging.debug(
        f"PR #{pr.number} word change analysis: "
        f"total_changes={total_changes}, unique_changes={unique_changes}, "
        f"repetition_ratio={repetition_ratio:.2f}, repeated_patterns={len(repeated_patterns)}"
    )
    
    if repeated_patterns:
        bt.logging.debug(f"Top repeated patterns: {repeated_patterns[:3]}")
    
    # Determine if it's a typo-only PR based on multiple signals
    typo_confidence = 0.0
    
    # Signal 1: Has typo keywords in title/description (30% weight)
    if has_typo_keyword:
        typo_confidence += 0.3
    
    # Signal 2: High repetition ratio - same changes repeated (40% weight)
    # If 70%+ of changes are repetitions of the same pattern, likely typo/rename
    if repetition_ratio >= 0.7:
        typo_confidence += 0.4
    elif repetition_ratio >= 0.5:
        typo_confidence += 0.2
    
    # Signal 3: Has repeated patterns (3+ occurrences) (30% weight)
    if has_repeated_patterns:
        # More repeated patterns = higher confidence
        pattern_score = min(len(repeated_patterns) / 3.0, 1.0)  # Cap at 1.0
        typo_confidence += 0.3 * pattern_score
    
    # Determine if it's typo-only
    is_typo_only = typo_confidence >= 0.6  # 60% confidence threshold
    
    # Additional check: If very few unique changes (1-2) repeated many times, definitely typo
    if unique_changes <= 2 and total_changes >= 5:
        is_typo_only = True
        typo_confidence = 1.0
    
    bt.logging.info(
        f"PR #{pr.number} typo detection: "
        f"is_typo_only={is_typo_only}, confidence={typo_confidence:.2f}, "
        f"has_keyword={has_typo_keyword}, repetition_ratio={repetition_ratio:.2f}, "
        f"repeated_patterns={len(repeated_patterns)}"
    )
    
    return is_typo_only, typo_confidence


def detect_translation_only_pr(pr: PullRequest) -> Tuple[bool, float]:
    """
    Detect if a PR contains only translation or AI-generated localization changes.

    Checks:
    1. PR title/description contains translation keywords
    2. Files match translation patterns or have translation-like patches
    3. Weighted ratio of translation files / changes
    4. Adaptive penalty scaling
    """
    if not pr.file_changes:
        return False, 0.0

    title_lower = pr.title.lower()
    desc_lower = (pr.description or '').lower()

    has_translation_keyword = any(
        keyword in title_lower or keyword in desc_lower
        for keyword in TRANSLATION_KEYWORDS
    )

    translation_files = 0
    translation_changes = 0
    total_changes = 0

    # Evaluate file by file
    for file_change in pr.file_changes:
        total_changes += file_change.changes

        # Check filename or patch content
        if is_translation_file(file_change.filename) or is_translation_content(file_change.patch):
            translation_files += 1
            translation_changes += file_change.changes

    # Compute ratios
    total_files = len(pr.file_changes)
    file_ratio = translation_files / total_files if total_files > 0 else 0.0
    change_ratio = translation_changes / total_changes if total_changes > 0 else 0.0
    translation_ratio = max(file_ratio, change_ratio)

    # Determine if PR is translation-only
    is_translation_only = (
        has_translation_keyword and translation_ratio >= MIN_TRANSLATION_RATIO_THRESHOLD
    ) or (
        translation_ratio >= 0.95  # Very strong signal
    )
    
    return is_translation_only, translation_ratio


def compute_translation_penalty(ratio: float) -> float:
    """
    Adaptive penalty scaling based on translation ratio.
    """
    if ratio >= 0.95:
        return 0.15
    elif ratio >= 0.9:
        return 0.25
    elif ratio >= 0.8:
        return 0.5
    return 1.0  # No penalty


def detect_whitespace_only_pr(pr: PullRequest) -> bool:
    """Detect PRs with only whitespace changes."""
    if not pr.file_changes:
        return False
    
    whitespace_files = 0
    for file_change in pr.file_changes:
        if not file_change.patch:
            continue
        
        # Remove all whitespace and compare
        lines = file_change.patch.split('\n')
        meaningful_changes = 0
        
        for line in lines:
            if line.startswith('+') or line.startswith('-'):
                # Strip all whitespace and check if there's actual content change
                stripped = re.sub(r'\s+', '', line[1:])
                if stripped:  # Has non-whitespace content
                    meaningful_changes += 1
        
        if meaningful_changes == 0:
            whitespace_files += 1
    
    return whitespace_files / len(pr.file_changes) > 0.8  # 80%+ whitespace-only


def detect_comment_only_pr(pr: PullRequest) -> bool:
    """Detect PRs with only comment additions."""
    comment_patterns = [
        r'^\s*#',           # Python comments
        r'^\s*//',          # JS/C++ comments
        r'^\s*/\*',         # Multi-line comment start
        r'^\s*\*',          # Multi-line comment middle
        r'^\s*"""',         # Python docstring
        r'^\s*<!--',        # HTML comment
    ]
    
    comment_lines = 0
    total_lines = 0
    
    for file_change in pr.file_changes:
        if not file_change.patch:
            continue
        
        lines = file_change.patch.split('\n')
        for line in lines:
            if line.startswith('+') and not line.startswith('+++'):
                total_lines += 1
                content = line[1:].strip()
                
                # Check if it's a comment
                if any(re.match(pattern, content) for pattern in comment_patterns):
                    comment_lines += 1
    
    if total_lines == 0:
        return False
    
    return comment_lines / total_lines > 0.9  # 90%+ comments


def detect_formatting_only_pr(pr: PullRequest) -> bool:
    """Detect PRs with only formatting changes."""
    title_lower = pr.title.lower()
    desc_lower = (pr.description or '').lower()
    
    has_format_keyword = any(
        kw in title_lower or kw in desc_lower
        for kw in FORMATTING_KEYWORDS
    )
    
    # Check if changes are small per line (typical of formatting)
    small_changes = 0
    total_files = 0
    
    for file_change in pr.file_changes:
        if not file_change.patch:
            continue
        
        total_files += 1
        lines = file_change.patch.split('\n')
        
        # Count lines with only minor changes (brackets, spaces, etc.)
        minor_change_lines = 0
        for line in lines:
            if line.startswith('+') or line.startswith('-'):
                content = line[1:].strip()
                # Check if it's just brackets, semicolons, spaces
                if re.match(r'^[\s\{\}\(\)\[\];,]*$', content) or len(content) < 5:
                    minor_change_lines += 1
        
        if minor_change_lines > len(lines) * 0.5:  # 50%+ minor changes
            small_changes += 1
    
    if total_files == 0:
        return False
    
    return has_format_keyword or (small_changes / total_files > 0.7)


def apply_spam_detection_penalties(pr: PullRequest) -> None:
    """
    Apply penalties to PRs detected as spam (typo-only or translation-only).
    
    This function modifies the PR's earned_score in-place.
    
    Args:
        pr: PullRequest object to check and penalize
    """
    original_score = pr.earned_score
    
    # Check for whitespace-only PR
    # Obviously this should be rejected by the owner but # better to include this logic
    is_whitespace_only = detect_whitespace_only_pr(pr)
    
    if is_whitespace_only:
        pr.set_earned_score(original_score * WHITESPACE_ONLY_PR_PENALTY)
        bt.logging.warning(
            f"SPAM DETECTION: PR #{mask_secret(str(pr.number))} in {mask_secret(pr.repository_full_name)} "
            f"detected as WHITESPACE-ONLY. "
            f"Score penalized: {original_score:.5f} -> {pr.earned_score:.5f} "
            f"(penalty: {WHITESPACE_ONLY_PR_PENALTY}x)"
        )
        original_score = pr.earned_score
        # Early return as score is reduced 0.05x, no need to reduce more
        return
    
    # Check for typo-only PR
    is_typo, typo_ratio = detect_typo_only_pr(pr)
    if is_typo:
        pr.set_earned_score(original_score * TYPO_ONLY_PR_PENALTY)
        bt.logging.warning(
            f"SPAM DETECTION: PR #{mask_secret(str(pr.number))} in {mask_secret(pr.repository_full_name)} "
            f"detected as TYPO-ONLY (ratio: {typo_ratio:.2f}). "
            f"Score penalized: {original_score:.5f} -> {pr.earned_score:.5f} "
            f"(penalty: {TYPO_ONLY_PR_PENALTY}x)"
        )
        original_score = pr.earned_score
        # Early return as score is reduced 0.1x, no need to reduce more
        return
    
    # Check for comment-only PR
    is_comment_only = detect_comment_only_pr(pr)
    if is_comment_only:
        pr.set_earned_score(original_score * COMMENT_ONLY_PR_PENALTY)
        bt.logging.warning(
            f"SPAM DETECTION: PR #{mask_secret(str(pr.number))} in {mask_secret(pr.repository_full_name)} "
            f"detected as Comment-ONLY. "
            f"Score penalized: {original_score:.5f} -> {pr.earned_score:.5f} "
            f"(penalty: {COMMENT_ONLY_PR_PENALTY}x)"
        )
        original_score = pr.earned_score
        # Early return as score is reduced 0.2x, no need to reduce more
        return
    
    # Check for translation-only PR
    is_translation, translation_ratio = detect_translation_only_pr(pr)
    if is_translation:
        penalty_factor = compute_translation_penalty(translation_ratio)
        pr.set_earned_score(original_score * penalty_factor)
        bt.logging.warning(
            f"SPAM DETECTION: PR #{mask_secret(str(pr.number))} in {mask_secret(pr.repository_full_name)} "
            f"detected as TRANSLATION-ONLY (ratio: {translation_ratio:.2f}). "
            f"Score penalized: {original_score:.5f} -> {pr.earned_score:.5f} "
            f"(penalty: {penalty_factor}x)"
        )
        original_score = pr.earned_score
    
    
    # Check for formatting/linting-only PR
    is_formatting_only = detect_formatting_only_pr(pr)
    if is_formatting_only:
        pr.set_earned_score(original_score * FORMATTING_ONLY_PR_PENALTY)
        bt.logging.warning(
            f"SPAM DETECTION: PR #{mask_secret(str(pr.number))} in {mask_secret(pr.repository_full_name)} "
            f"detected as Formatting/Linting-ONLY. "
            f"Score penalized: {original_score:.5f} -> {pr.earned_score:.5f} "
            f"(penalty: {FORMATTING_ONLY_PR_PENALTY}x)"
        )
        original_score = pr.earned_score
        # Early return as score is reduced 0.15x, no need to reduce more
        return
    
    # If no spam detected, log that it passed
    bt.logging.info(
        f"PR #{mask_secret(str(pr.number))} passed spam detection checks "
        f"(typo_ratio: {typo_ratio:.2f}, translation_ratio: {translation_ratio:.2f})"
    )