import bittensor as bt

from gittensor.classes import PullRequest
from gittensor.constants import (
    TYPO_ONLY_PENALTY,
)
from gittensor.validator.utils.spam_detection import (
    is_typo_only_pr,
)
from gittensor.utils.utils import mask_secret

def detect_typo_only_pr(pr: PullRequest) -> bool:
    if not pr.file_changes:
        return False
    
    file_patches = [
        fc.patch for fc in pr.file_changes
        if fc.patch and isinstance(fc.patch, str)
    ]

    if not file_patches:
        return False
    
    is_typo_only = is_typo_only_pr(file_patches)
    return is_typo_only

def apply_typo_detection_penalties(pr: PullRequest, uid: int) -> None:
    original_score = pr.earned_score

    is_typo = detect_typo_only_pr(pr)
    if is_typo:
        pr.set_earned_score(TYPO_ONLY_PENALTY)
        bt.logging.debug(
            f"Miner UID: {uid} "
            f"TYPO DETECTION: PR #{mask_secret(str(pr.number))} in {mask_secret(pr.repository_full_name)} "
            f"Score penalized: {original_score:.5f} -> {pr.earned_score:.5f} "
        )
