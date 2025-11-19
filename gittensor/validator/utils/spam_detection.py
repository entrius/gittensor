import re
from typing import List, Tuple
from Levenshtein import distance, ratio 
from gittensor.constants import (
    TYPO_RATIO_THRESHOLD,
    MAX_TYPO_FILE_PATCH_LINES,
    TYPO_MAX_DIST,
    TYPO_MIN_SIM
)

def tokenize(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9_'-]+", text)

def token_pair_typo(o: str, n: str, max_dist: int, min_sim: float) -> bool:
    dist = distance(o, n)
    sim = ratio(o, n)
    return dist <= max_dist or sim >= min_sim

def is_token_typo(old: str, new: str, max_dist=TYPO_MAX_DIST, min_sim=TYPO_MIN_SIM) -> bool:
    old_tokens = tokenize(old)
    new_tokens = tokenize(new)

    if len(old_tokens) != len(new_tokens):
        return False

    return all(token_pair_typo(o, n, max_dist, min_sim)
            for o, n in zip(old_tokens, new_tokens))

def extract_line_pairs_from_patch(patch: str) -> List[Tuple[str, str]]:
    removed = []
    added = []
    
    for line in patch.split("\n"):
        if line.startswith("-") and not line.startswith(("--", "---")):
            removed.append(line[1:])
        elif line.startswith("+") and not line.startswith(("++", "+++")):
            added.append(line[1:])
    
    return list(zip(removed, added))

def is_typo_only_patch(patch: str) -> bool:
    pairs = extract_line_pairs_from_patch(patch)

    if not pairs:
        # Pairs are our only indicator of typos
        return False

    typo_like = 0

    for old, new in pairs:
        if is_token_typo(old,new):
             typo_like +=1

    return (typo_like / len(pairs)) >= TYPO_RATIO_THRESHOLD

def is_typo_only_pr(file_patches: List[str]) -> bool:
    if not file_patches:
        return False

    for patch in file_patches:
        patch_line_count = patch.count("\n")
        
        # Performance & correctness cutoff
        if patch_line_count > MAX_TYPO_FILE_PATCH_LINES:
            return False
        
        if not is_typo_only_patch(patch):
            return False

    return True
