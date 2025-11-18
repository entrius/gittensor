# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Spam Detection Utilities

Detects and penalizes low-quality PRs:
- Typo-only PRs (minor spelling/grammar fixes)
"""

import bittensor as bt
from typing import Tuple

from gittensor.classes import PullRequest
from gittensor.constants import (
    TYPO_ONLY_PENALTY,
    TYPO_KEYWORDS,
    TYPO_RATIO_THRESHOLD
)
from gittensor.validator.utils.spam_detection import (
    is_typo_only_pr,
)
from gittensor.utils.utils import mask_secret


def detect_typo_only_pr(pr: PullRequest) -> Tuple[bool, float]:
    """
    Detect if a PR contains only typo/formatting fixes using smart pattern analysis.
    """
    
    if not pr.file_changes:
        return False, 0.0
    
    file_patches = [
        fc.patch for fc in pr.file_changes
        if fc.patch and isinstance(fc.patch, str)
    ]

    if not file_patches:
        return False, 0.0
    
    # Core typo detection using new algorithm
    is_typo_only = is_typo_only_pr(file_patches)
    
    # Compute confidence score
    # Base confidence from patch classification
    confidence = 1.0 if is_typo_only else 0.0
    
    # Optional boost from PR title/description keywords
    title_lower = pr.title.lower()
    desc_lower = (pr.description or "").lower()

    has_keyword = any(
        keyword in title_lower or keyword in desc_lower
        for keyword in TYPO_KEYWORDS
    )

    if is_typo_only and has_keyword:
        confidence = min(1.0, confidence + 0.1)

    return is_typo_only


def apply_typo_detection_penalties(pr: PullRequest, uid: int) -> None:
    """
    Apply penalties to PRs detected as spam (typo-only).
    """
    original_score = pr.earned_score

    is_typo = detect_typo_only_pr(pr)
    if is_typo:
        pr.set_earned_score(TYPO_ONLY_PENALTY)
        bt.logging.debug(
            f"Miner UID: {uid} "
            f"TYPO DETECTION: PR #{mask_secret(str(pr.number))} in {mask_secret(pr.repository_full_name)} "
            f"Score penalized: {original_score:.5f} -> {pr.earned_score:.5f} "
        )
