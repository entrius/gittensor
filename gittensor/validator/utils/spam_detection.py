import difflib
import re
from typing import List, Tuple
from gittensor.constants import TYPO_RATIO_THRESHOLD, MAX_TYPO_FILE_PATCH_LINES
from Levenshtein import distance, ratio 

def tokenize(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9_'-]+", text)

def introduces_code_symbols(text: str) -> bool:
    return bool(re.search(r"[{}\[\]()<>=+\-/*&|^%#!?:]", text))

def contains_keywords(text: str) -> bool:
    keywords = (
        r"\b(return|if|else|for|while|switch|case|break|continue|do|"
        r"class|def|const|let|var|function|fn|try|catch|throw|lambda)\b"
    )
    return bool(re.search(keywords, text))

def is_comment_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(("//", "///", "/*", "*", "#", "<!--", "'''", '"""'))

def token_pair_typo(o: str, n: str, max_dist: int, min_sim: float) -> bool:
    dist = distance(o, n)
    sim = ratio(o, n)
    return dist <= max_dist or sim >= min_sim

def is_comment_typo(old: str, new: str) -> bool:
    """Detect typo-like changes inside comments."""
    if not (is_comment_line(old) and is_comment_line(new)):
        return False

    old_words = tokenize(old)
    new_words = tokenize(new)

    sm = difflib.SequenceMatcher(None, old_words, new_words)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():

        if tag == "equal":
            continue

        # Insertions/deletions in comments ≠ code change
        if tag in ("insert", "delete"):
            continue

        if tag == "replace":
            old_segment = old_words[i1:i2]
            new_segment = new_words[j1:j2]

            for o, n in zip(old_segment, new_segment):
                # Comment typo thresholds are slightly more lenient:
                if not token_pair_typo(o, n, max_dist=3, min_sim=0.7):
                    return False

    return True

def classify_change(old: str, new: str) -> str:
    def is_token_typo(old: str, new: str, max_dist=2, min_sim=0.75) -> bool:
        old_tokens = tokenize(old)
        new_tokens = tokenize(new)

        if len(old_tokens) != len(new_tokens):
            return False

        return all(token_pair_typo(o, n, max_dist, min_sim)
               for o, n in zip(old_tokens, new_tokens))
    
    if is_comment_line(old) and is_comment_line(new):

        if is_comment_typo(old, new):
            return "comment_typo"

        if is_token_typo(old, new):
            return "typo"

        return "safe_small_edit"

    if old.strip() == new.strip():
        return "formatting"

    old_alpha = re.sub(r"[A-Za-z0-9]+", "", old)
    new_alpha = re.sub(r"[A-Za-z0-9]+", "", new)
    if old_alpha == new_alpha:
        return "punctuation"

    if is_token_typo(old, new):
        return "typo"
    
    line_dist = distance(old, new)
    line_sim = ratio(old, new)

    if line_dist <= 3 and line_sim >= 0.85:
        if not introduces_code_symbols(new) and not contains_keywords(new):
            return "safe_small_edit"
        
    if '"' in old or '"' in new:
        if is_comment_typo(old, new):
            return "comment_typo"

    if not is_comment_line(old) and not is_comment_line(new):
        in_string = ('"' in old or "'" in old or '"' in new or "'" in new)

        if not in_string:
            if introduces_code_symbols(new) or contains_keywords(new):
                return "danger"

    return "unknown"

def extract_line_pairs_from_patch(patch: str) -> List[Tuple[str, str]]:
    lines = patch.split("\n")
    removed = []
    added = []

    for line in lines:
        if line.startswith(("@@", "+++", "---")):
            continue
        if line.startswith("-") and not line.startswith("--"):
            removed.append(line[1:])
        elif line.startswith("+") and not line.startswith("++"):
            added.append(line[1:])

    count = min(len(removed), len(added))
    return list(zip(removed[:count], added[:count]))

def is_typo_only_patch(patch: str) -> bool:
    pairs = extract_line_pairs_from_patch(patch)

    if not pairs:
        lines = patch.split("\n")
        has_added = any(l.startswith("+") and not l.startswith("++") for l in lines)
        has_removed = any(l.startswith("-") and not l.startswith("--") for l in lines)

        # No added/removed = no content changes → typo-like
        if not has_added and not has_removed:
            return True

        # Any actual content addition/deletion without pairing → not a typo
        return False

    typo_like = 0
    total = len(pairs)

    for old, new in pairs:
        category = classify_change(old, new)

        if category in ("formatting", "punctuation", "comment_typo", "typo", "safe_small_edit"):
            typo_like += 1
        elif category == "danger":
            return False

    return (typo_like / total) >= TYPO_RATIO_THRESHOLD

def is_typo_only_pr(file_patches: List[str]) -> bool:
    if not file_patches:
        return False

    for patch in file_patches:
        patch_line_count = patch.count("\n")
        
        # ❗ Performance & correctness cutoff
        if patch_line_count > MAX_TYPO_FILE_PATCH_LINES:
            return False
        
        if not is_typo_only_patch(patch):
            return False

    return True
