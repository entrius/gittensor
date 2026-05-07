python
# Solution: [Bug] `git issues list --id` can crash on string/invalid bounty fields after shared `_fill_percent()`
# Repo: entrius/gittensor Issue #1078
# Generated: 2026-05-08

def _safe_int(value, default=0):
    """Safely convert a value to int, returning default on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _fill_percent(current, target):
    """Compute fill percentage. Both args must be ints."""
    if target <= 0:
        return 0.0
    return min(100.0, (current / target) * 100.0)

def render_single_issue(issue):
    """Render a single issue from `git issues list --id`, safely handling bounty fields."""
    # Safely parse bounty fields — raw issue data may contain strings or invalid values.
    # The table view already does this via int() inside try/except; match that behavior.
    bounty_amount = _safe_int(issue.get("bounty_amount"))
    target_bounty = _safe_int(issue.get("target_bounty"))

    pct = _fill_percent(bounty_amount, target_bounty)

    # Format output lines for the single-issue view
    lines = [
        f"Issue    : {issue.get('id', 'N/A')}",
        f"Title    : {issue.get('title', 'N/A')}",
        f"Status   : {issue.get('status', 'N/A')}",
        f"Bounty   : {bounty_amount} / {target_bounty}",
        f"Filled   : {pct:.1f}%",
    ]
    return "\n".join(lines)


# ---- Minimal self-test ----
if __name__ == "__main__":
    # Case 1: valid numeric bounty
    ok = {"id": 1, "title": "Good", "status": "open", "bounty_amount": 50, "target_bounty": 100}
    print(render_single_issue(ok))
    print("---")

    # Case 2: string bounty (previously crashed)
    bad = {"id": 2, "title": "Bad", "status": "open", "bounty_amount": "50", "target_bounty": "100"}
    print(render_single_issue(bad))
    print("---")

    # Case 3: missing / None bounty fields
    missing = {"id": 3, "title": "Missing", "status": "closed"}
    print(render_single_issue(missing))
    print("---")

    # Case 4: garbage string bounty
    garbage = {"id": 4, "title": "Garbage", "status": "open", "bounty_amount": "abc", "target_bounty": None}
    print(render_single_issue(g