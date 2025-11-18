import difflib
import re
from typing import List, Tuple
from gittensor.validator.constants import TYPO_RATIO_THRESHOLD

def levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein distance."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            insert = curr[j-1] + 1
            delete = prev[j] + 1
            replace = prev[j-1] + (ca != cb)
            curr.append(min(insert, delete, replace))
        prev = curr

    return prev[-1]

def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()

def tokenize(text: str) -> List[str]:
    """Split text by alphanumeric boundaries."""
    return re.findall(r"[A-Za-z0-9_'-]+", text)

def introduces_code_symbols(text: str) -> bool:
    """Detect if a line introduces new code structures."""
    return bool(re.search(r"[{}\[\]()<>=+\-/*&|^%#!?:]", text))

def contains_keywords(text: str) -> bool:
    """Detect if a line contains programming keywords."""
    keywords = (
        r"\b(return|if|else|for|while|switch|case|break|continue|do|"
        r"class|def|const|let|var|function|fn|try|catch|throw|lambda)\b"
    )
    return bool(re.search(keywords, text))

def is_comment_line(line: str) -> bool:
    """Detect comments for languages: C#, Java, JS, TS, C++, Rust, Go, Python, HTML."""
    stripped = line.strip()
    return stripped.startswith(("//", "///", "/*", "*", "#", "<!--", "'''", '"""'))


# ---------------------------------------------------------
# Comment-aware typo detection
# ---------------------------------------------------------
def is_comment_typo(old: str, new: str) -> bool:
    """Detect typo-like changes inside comments."""
    if not (is_comment_line(old) and is_comment_line(new)):
        return False

    old_words = tokenize(old)
    new_words = tokenize(new)

    sm = difflib.SequenceMatcher(None, old_words, new_words)
    all_ok = True

    for tag, i1, i2, j1, j2 in sm.get_opcodes():

        if tag == "equal":
            continue

        # Insertions/deletions in comments ≠ code change
        if tag in ("insert", "delete"):
            continue

        if tag == "replace":
            old_segment = old_words[i1:i2]
            new_segment = new_words[j1:j2]

            # Compare replaced words pairwise
            for o, n in zip(old_segment, new_segment):
                dist = levenshtein(o, n)
                sim = similarity(o, n)

                # Allow up to 3-character spelling change
                if dist <= 3 or sim >= 0.7:
                    continue

                all_ok = False

    return all_ok

# ---------------------------------------------------------
# Core line classification
# ---------------------------------------------------------

def classify_change(old: str, new: str) -> str:
    """
    Classifications:
        - formatting
        - punctuation
        - comment_typo
        - typo
        - safe_small_edit
        - danger
        - unknown
    """

    # SPECIAL HANDLING FOR COMMENT LINES
    if is_comment_line(old) and is_comment_line(new):
        # 1. Comment-aware typo detection
        if is_comment_typo(old, new):
            return "comment_typo"

        # 2. Try normal typo detection inside comments
        old_tokens = tokenize(old)
        new_tokens = tokenize(new)
        if len(old_tokens) == len(new_tokens):
            ok = True
            for o, n in zip(old_tokens, new_tokens):
                if o == n:
                    continue

                dist = levenshtein(o, n)
                sim = similarity(o, n)

                if dist <= 2 or sim >= 0.75:
                    continue

                ok = False
            if ok:
                return "typo"

        # 3. Comments cannot ever be "danger"
        # A large change in a comment is still harmless → safe_small_edit
        return "safe_small_edit"

    # Formatting-only change
    if old.strip() == new.strip():
        return "formatting"

    # Punctuation-only change
    old_alpha = re.sub(r"[A-Za-z0-9]+", "", old)
    new_alpha = re.sub(r"[A-Za-z0-9]+", "", new)
    if old_alpha == new_alpha:
        return "punctuation"

    # Word-level typo detection
    old_tokens = tokenize(old)
    new_tokens = tokenize(new)

    if len(old_tokens) == len(new_tokens):
        ok = True
        for o, n in zip(old_tokens, new_tokens):
            if o == n:
                continue

            dist = levenshtein(o, n)
            sim = similarity(o, n)

            if dist <= 2 or sim >= 0.75:
                continue

            ok = False

        if ok:
            return "typo"

    # Small safe edit (line-level)
    line_dist = levenshtein(old, new)
    line_sim = similarity(old, new)

    if line_dist <= 3 and line_sim >= 0.85:
        if not introduces_code_symbols(new) and not contains_keywords(new):
            return "safe_small_edit"
        
    # If line is a string literal assignment, treat like comment
    if '"' in old or '"' in new:
        # attempt typo classification
        if is_comment_typo(old, new):
            return "comment_typo"

    # Dangerous change (code logic change)
    if not is_comment_line(old) and not is_comment_line(new):
        # ignore code symbols if inside a string
        in_string = ('"' in old or "'" in old or '"' in new or "'" in new)

        if not in_string:
            if introduces_code_symbols(new) or contains_keywords(new):
                return "danger"

    return "unknown"

# Patch parsing + PR evaluation

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

        # Added-only patch
        if has_added and not has_removed:
            return False

        # Removed-only patch
        if has_removed and not has_added:
            return False

        # If neither added nor removed (empty patch), harmless
        return True

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
        if not is_typo_only_patch(patch):
            return False

    return True
