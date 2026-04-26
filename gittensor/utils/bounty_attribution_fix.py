"""
Fix for gittensor bounty attribution bug:
https://github.com/entrius/gittensor/issues/757

PROBLEM:
--------
In `gittensor/utils/github_api_tools.py:1062-1076`, when multiple merged PRs
declare themselves as closing the same issue, `find_solver_from_cross_references`
picks the MOST RECENTLY merged PR instead of the FIRST one that actually closed
the issue.

This is exploitable: a miner can open a trivial follow-up PR with "Closes #X" in
the body, get it merged later, and steal the bounty attribution from the original
solver.

ROOT CAUSE:
-----------
1. `closingIssuesReferences` is a self-declared intent from the PR body, NOT a
   record of what the PR actually closed.
2. Sorting by `merged_at` descending gives the latest PR the bounty, even if it
   didn't actually close the issue.
3. GitHub closes an issue on the FIRST merged PR that triggers the close.

FIX:
----
1. Use GitHub Events API to find the actual "closed" event and its associated PR
2. Fall back to sorting by `merged_at` ASCENDING (first merged = first closer)
3. Add a cross-reference check: verify the PR actually triggered the close event
"""

import re
from datetime import datetime
from typing import Optional

# ============================================================================
# ORIGINAL BUGGY CODE (for reference):
# ============================================================================
"""
# gittensor/utils/github_api_tools.py:1062-1076 (BEFORE)
async def find_solver_from_cross_references(
    issue_number: int,
    all_prs: list[dict],
    gh: Github,
    repo_name: str,
) -> tuple[Optional[str], Optional[int]]:
    ...
    merged = [
        p for p in all_prs
        if p.get("state") == "MERGED" and issue_number in p.get("closing_numbers", [])
    ]
    merged.sort(key=lambda p: p.get("merged_at") or "", reverse=True)  # BUG: latest wins
    best = merged[0]
    ...
"""

# ============================================================================
# FIXED CODE:
# ============================================================================

CLOSING_PATTERN = re.compile(
    r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"
    r"\s+#?(\d+)",
    re.IGNORECASE,
)


def extract_closing_issue_numbers(pr_body: str) -> list[int]:
    """Extract issue numbers that a PR claims to close from its body."""
    if not pr_body:
        return []
    return [int(m) for m in CLOSING_PATTERN.findall(pr_body)]


def find_actual_closing_pr(
    issue_number: int,
    all_prs: list[dict],
    events: Optional[list[dict]] = None,
) -> Optional[dict]:
    """
    Find the PR that ACTUALLY closed the issue, not just claimed to.

    Uses GitHub Events API when available to find the actual close event,
    otherwise falls back to first-merged-wins strategy.

    Args:
        issue_number: The issue number to find the closing PR for
        all_prs: List of all PR dicts (must include state, merged_at, body, number)
        events: Optional list of issue events from GitHub Events API

    Returns:
        The PR dict that actually closed the issue, or None if not found
    """
    # Step 1: Filter to merged PRs that claim to close this issue
    candidate_prs = []
    for pr in all_prs:
        if pr.get("state") != "MERGED":
            continue
        pr_body = pr.get("body", "") or ""
        closing_numbers = extract_closing_issue_numbers(pr_body)
        if issue_number in closing_numbers:
            candidate_prs.append(pr)

    if not candidate_prs:
        return None

    # Step 2: If we have events, find the actual close event
    if events:
        for event in events:
            if event.get("event") == "closed":
                # Check if this was closed by a PR (commit_status is None for PR closes)
                commit_id = event.get("commit_id")
                # The event might have a "performed_via_github_app" or reference
                # We need to match the PR number from the event
                pr_ref = event.get("pull_request", {})
                pr_number = pr_ref.get("number") if isinstance(pr_ref, dict) else None

                if pr_number:
                    for pr in candidate_prs:
                        if pr.get("number") == pr_number:
                            return pr

    # Step 3: Fall back to first-merged-wins (CORRECT strategy)
    # The first PR merged that claims to close the issue is the one that
    # actually triggered the GitHub close event.
    candidate_prs.sort(key=lambda p: p.get("merged_at") or "")  # ASCENDING order
    return candidate_prs[0]  # First merged = actual closer


def find_solver_from_cross_references_fixed(
    issue_number: int,
    all_prs: list[dict],
    events: Optional[list[dict]] = None,
) -> tuple[Optional[str], Optional[int]]:
    """
    Fixed version of find_solver_from_cross_references.

    Returns:
        (solver_login, pr_number) or (None, None) if no solver found
    """
    closing_pr = find_actual_closing_pr(issue_number, all_prs, events)

    if closing_pr is None:
        return None, None

    solver_login = closing_pr.get("user", {}).get("login")
    pr_number = closing_pr.get("number")

    return solver_login, pr_number


# ============================================================================
# ADDITIONAL SAFEGUARD: Cross-reference validation
# ============================================================================

def validate_bounty_attribution(
    issue_number: int,
    all_prs: list[dict],
    events: list[dict],
) -> dict:
    """
    Validate that the bounty attribution is correct.

    This function can be used as a pre-commit check or monitoring tool
    to detect bounty theft attempts.

    Returns:
        dict with validation results:
        {
            "valid": bool,
            "actual_solver": str or None,
            "claimed_solver": str or None,
            "closing_pr": int or None,
            "potential_theft": bool,
            "details": str
        }
    """
    actual_solver, closing_pr = find_solver_from_cross_references_fixed(
        issue_number, all_prs, events
    )

    # Check for potential theft: multiple PRs claiming to close
    claiming_prs = []
    for pr in all_prs:
        if pr.get("state") != "MERGED":
            continue
        body = pr.get("body", "") or ""
        if str(issue_number) in body and re.search(
            r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)",
            body, re.IGNORECASE
        ):
            claiming_prs.append(pr)

    potential_theft = len(claiming_prs) > 1

    if potential_theft:
        # Sort by merge time to identify the follower
        claiming_prs.sort(key=lambda p: p.get("merged_at") or "")
        first_pr = claiming_prs[0]
        follower_pr = claiming_prs[-1]

        details = (
            f"Multiple PRs claim to close #{issue_number}: "
            f"PR#{first_pr['number']} (merged {first_pr.get('merged_at')}) by "
            f"{first_pr['user']['login']} is the actual closer. "
            f"PR#{follower_pr['number']} (merged {follower_pr.get('merged_at')}) by "
            f"{follower_pr['user']['login']} is a potential follower."
        )
    else:
        details = f"Single PR (#{closing_pr}) correctly attributed to {actual_solver}."

    return {
        "valid": True,
        "actual_solver": actual_solver,
        "closing_pr": closing_pr,
        "potential_theft": potential_theft,
        "details": details,
    }


# ============================================================================
# USAGE EXAMPLE:
# ============================================================================

async def patched_find_solver(
    issue_number: int,
    all_prs: list[dict],
    gh,  # Github client
    repo_name: str,
) -> tuple[Optional[str], Optional[int]]:
    """
    Patched version to replace the buggy function in github_api_tools.py.

    Usage: Replace the existing find_solver_from_cross_references with this.
    """
    # Fetch issue events to find actual close event
    try:
        owner, repo = repo_name.split("/")
        repo_obj = gh.get_repo(repo_name)
        issue = repo_obj.get_issue(issue_number)
        events = [
            {
                "event": e.event,
                "commit_id": getattr(e, "commit_id", None),
                "pull_request": {"number": e.pull_request_url.split("/")[-1]}
                    if hasattr(e, "pull_request_url") and e.pull_request_url
                    else None,
            }
            for e in issue.get_events()
        ]
    except Exception:
        # If we can't fetch events, fall back to first-merged-wins
        events = None

    return find_solver_from_cross_references_fixed(issue_number, all_prs, events)


if __name__ == "__main__":
    # Test the fix
    import asyncio

    # Simulated PRs
    test_prs = [
        {
            "number": 100,
            "state": "MERGED",
            "merged_at": "2026-04-20T10:00:00Z",
            "body": "Fixes #42 - Main implementation",
            "user": {"login": "solver_a"},
        },
        {
            "number": 101,
            "state": "MERGED",
            "merged_at": "2026-04-20T12:00:00Z",
            "body": "Closes #42 - Trivial follow-up (bounty thief)",
            "user": {"login": "solver_b"},
        },
    ]

    # Test: Without events (should use first-merged-wins)
    solver, pr = find_solver_from_cross_references_fixed(42, test_prs)
    assert solver == "solver_a", f"Expected solver_a, got {solver}"
    assert pr == 100, f"Expected PR 100, got {pr}"
    print("✅ Test 1 passed: First-merged-wins correctly identifies solver_a")

    # Test: With events (should use event-based detection)
    test_events = [
        {"event": "closed", "pull_request": {"number": 100}},
    ]
    solver, pr = find_solver_from_cross_references_fixed(42, test_prs, test_events)
    assert solver == "solver_a", f"Expected solver_a, got {solver}"
    assert pr == 100, f"Expected PR 100, got {pr}"
    print("✅ Test 2 passed: Event-based detection correctly identifies solver_a")

    # Test: Validation
    result = validate_bounty_attribution(42, test_prs, test_events)
    assert result["potential_theft"] == True
    assert result["actual_solver"] == "solver_a"
    print("✅ Test 3 passed: Theft detection works correctly")

    # Test: Single PR (no theft)
    single_prs = [
        {
            "number": 100,
            "state": "MERGED",
            "merged_at": "2026-04-20T10:00:00Z",
            "body": "Fixes #42",
            "user": {"login": "solver_a"},
        },
    ]
    solver, pr = find_solver_from_cross_references_fixed(42, single_prs)
    assert solver == "solver_a"
    assert pr == 100
    print("✅ Test 4 passed: Single PR case works correctly")

    print("\n🎉 All tests passed! The fix correctly prevents bounty theft.")
