#!/usr/bin/env python3
"""
Upgrade all 10 open PRs to v3 body style (terminal evidence + how-to-verify + post-merge).

Usage:
  python3 upgrade_all_prs.py               # Upgrade all PRs
  python3 upgrade_all_prs.py --dry-run      # Preview only
  python3 upgrade_all_prs.py --pr 1129      # Single PR
"""

import argparse, subprocess, sys, os
sys.path.insert(0, os.path.dirname(__file__))
from pr_body_builder import build, update_pr_body

PRS = [
    {
        "pr": 1092,
        "issue": 1082,
        "title": "fix: Mirror PR adapter crash on null numeric fields (#1082)",
        "root_cause": (
            "`MirrorPRAdapter.__parse_numeric_value()` calls `getattr(pr, field, None)` "
            "on fields like `additions`, `deletions`, `changed_files`. When these are `None` "
            "(e.g., for mirrored PRs from certain providers), `int(None)` raises `TypeError`. "
            "The adapter then fails to parse the entire PR, silently dropping it from scoring."
        ),
        "impact": (
            "Mirrored PRs from providers that return null numeric fields are silently dropped "
            "from miner scoring. Validator sees fewer PRs than actually exist."
        ),
        "solution": [
            "Add `None` guard in `__parse_numeric_value()`: return 0 if value is None",
            "Add test for null numeric fields in MirrorPRAdapter.parse_pr()",
        ],
        "why": "Mirror PR adapter is used by alternative GitHub providers; null fields are valid per GraphQL schema",
        "tests": [
            "ruff check",
            "ruff format --check",
            "pyright",
            "pytest tests/validator/test_mirror_pr_adapter.py -q",
        ],
        "live_verif": [
            "python3 -c \"from gittensor.validator import MirrorPRAdapter; m=MirrorPRAdapter(); print(type(m).__name__)\"",
        ],
        "how_to_verify": [
            "Run `pytest tests/validator/test_mirror_pr_adapter.py::test_parse_pr_null_numeric` — must pass",
            "Verify that `MirrorPRAdapter.__parse_numeric_value(None)` returns 0 (not TypeError)",
        ],
        "post_merge": [
            "Confirm PRs from mirrored providers appear in scoring after next cycle",
            "Monitor validator log for `MirrorPRAdapter` warnings",
        ],
        "edge_cases": [
            "All numeric fields null simultaneously",
            "Mixed null and valid numeric fields in same PR",
            "Non-integer numeric values from provider (e.g., float strings)",
        ],
    },
    {
        "pr": 1101,
        "issue": 985,
        "title": "fix: line-count score extensionless files instead of skipping (#985)",
        "root_cause": (
            "`get_line_count()` in scoring uses `os.path.splitext(path)[1]` to filter file types. "
            "Extensionless files produce an empty string `''`, which doesn't match any known extension — "
            "so they're skipped entirely, undercounting the PR's true size for code density scoring."
        ),
        "impact": (
            "PRs containing extensionless files (e.g., `Dockerfile`, `Makefile`, `Procfile`) get "
            "a lower code density score than deserved. Miner loses up to 1.5x multiplier unfairly."
        ),
        "solution": [
            "Fall back to line-count scoring when extension is empty (extensionless files)",
            "Count extensionless files as code lines instead of skipping them",
        ],
        "why": "Extensionless files are often important config/build files. Skipping them distorts code density scoring",
        "tests": [
            "ruff check",
            "ruff format --check",
            "pyright",
            "pytest tests/validator/test_scoring.py::test_get_line_count_extensionless -q",
        ],
        "how_to_verify": [
            "Run the test: `pytest tests/validator/test_scoring.py::test_get_line_count_extensionless`",
            "Verify extensionless files appear in the line count output",
        ],
        "post_merge": [
            "Check that extensionless files are now counted in code density",
            "Compare code density score before/after for a PR with Makefile/Dockerfile",
        ],
        "edge_cases": [
            "Files with only dot prefix (e.g., `.gitignore`, `.env`) — these have extensions",
            "Files with no content (0 lines) — should still be counted as 0",
            "Binary extensionless files — line count will be 0 naturally",
        ],
    },
    {
        "pr": 1102,
        "issue": 1046,
        "title": "fix: distinguish PAT validation error messages (#1046)",
        "root_cause": (
            "`validate_pat_token()` in `github_api_tools.py` returns generic `'token invalid'` "
            "for both 401 (bad token) and 403 (rate limit / insufficient scope). Miners can't tell "
            "whether their PAT needs a scope change or is just expiring."
        ),
        "impact": (
            "Miners waste time regenerating tokens that are actually valid but rate-limited. "
            "Validator can't distinguish transient failures from permanent auth failures."
        ),
        "solution": [
            "Parse GitHub API error response body for specific error type",
            "Return distinct messages: 'token invalid (bad credentials)' vs 'token valid but rate limited'",
        ],
        "tests": [
            "ruff check",
            "ruff format --check",
            "pyright",
            "pytest tests/utils/test_github_api_tools.py::test_validate_pat_error_messages -q",
        ],
        "how_to_verify": [
            "Run `pytest tests/utils/test_github_api_tools.py::test_validate_pat_error_messages`",
            "Verify distinct error messages for 401 vs 403 responses",
        ],
        "post_merge": [
            "Check miner output shows differentiated PAT error messages",
            "Monitor validator log for PAT validation warnings",
        ],
        "edge_cases": [
            "Expired token vs never-valid token — both 401 but different user-facing messages",
            "Token with insufficient scope (403) vs rate-limited (403) — different GraphQL errors",
            "Empty token string — should fail fast before API call",
        ],
    },
    {
        "pr": 1103,
        "issue": 989,
        "title": "fix: include excluded validator details in error (#989)",
        "root_cause": (
            "When a validator is excluded from scoring (e.g., low trust score), the error/log "
            "only shows `'validator excluded'` without the validator's identity or reason. "
            "Operators can't audit why validators are excluded."
        ),
        "impact": (
            "Validator operators can't debug exclusion decisions. Trust score tuning is opaque."
        ),
        "solution": [
            "Include validator hotkey + trust score in exclusion log message",
            "Add structured logging for each exclusion decision",
        ],
        "tests": [
            "ruff check",
            "ruff format --check",
            "pyright",
            "pytest tests/validator/test_validator_scoring.py::test_excluded_validator_logging -q",
        ],
        "how_to_verify": [
            "Run the test to verify exclusion log contains validator identity",
            "Check log format includes both hotkey and trust score",
        ],
        "post_merge": [
            "Verify validator log shows `excluded: 5F... (trust_score=0.3)` for each exclusion",
            "Confirm no PII leaked in exclusion logs",
        ],
        "edge_cases": [
            "Validator excluded with score exactly at threshold boundary",
            "All validators excluded simultaneously",
            "Validator hotkey None (edge case)",
        ],
    },
    {
        "pr": 1125,
        "issue": 1089,
        "title": "fix: preserve null author_github_id (#1089)",
        "root_cause": (
            "When a GitHub user has no public GitHub ID (e.g., deleted account, or user ID not "
            "exposed by the fork's GraphQL), `author_github_id` is `None`. The scoring code "
            "converts this to 0 or empty string, breaking downstream lookups."
        ),
        "impact": (
            "PRs from anonymous/deleted GitHub users get incorrect author resolution. "
            "Cross-reference lookups fail because `author_github_id=0` matches no real user."
        ),
        "solution": [
            "Allow null author_github_id through the pipeline without coercion",
            "Handle None in downstream comparison logic",
        ],
        "tests": [
            "ruff check",
            "ruff format --check",
            "pyright",
            "pytest tests/validator/test_author_resolution.py::test_null_author_github_id -q",
        ],
        "how_to_verify": [
            "Run the test for null author_github_id handling",
            "Verify that None propagates correctly through scoring pipeline",
        ],
        "post_merge": [
            "Check PRs from deleted GitHub accounts are scored correctly",
            "Verify no TypeError for null author_github_id in production",
        ],
        "edge_cases": [
            "author_github_id is None but author_github_username is present",
            "PRs from organization accounts (no user ID)",
            "Migration from old to new GitHub ID format",
        ],
    },
    {
        "pr": 1126,
        "issue": 1017,
        "title": "fix: use last:50 instead of first:50 for solver lookup (#1017)",
        "root_cause": (
            "Solver cross-reference lookup uses `first:50` to fetch PRs, which returns the OLDEST "
            "PRs first. The solver's most recent PR (the one affecting current scoring) is likely "
            "at the end of the list — often beyond the 50-item window, so it's missed entirely."
        ),
        "impact": (
            "Solver cross-reference resolution misses recent PRs, leading to stale solver data "
            "and incorrect scoring of solver contributions."
        ),
        "solution": [
            "Change `first:50` to `last:50` in GraphQL query for solver repository lookup",
            "This returns the most recent 50 PRs, where the solver's active PRs live",
        ],
        "why": "Solvers are scored on recent PRs. `first:50` returns oldest PRs — wrong direction for relevance",
        "tests": [
            "ruff check",
            "ruff format --check",
            "pyright",
            "pytest tests/utils/test_solver_lookup.py::test_solver_lookup_last_50 -q",
        ],
        "how_to_verify": [
            "Run the solver lookup test to confirm last:50 returns recent PRs",
            "Verify the GraphQL query uses `last:50` not `first:50`",
        ],
        "post_merge": [
            "Check solver cross-reference now finds recent PRs correctly",
            "Verify solver scoring improved for active solvers",
        ],
        "edge_cases": [
            "Solver repository has <50 PRs total (last:50 works same as first:50)",
            "Repository with only old PRs and no recent activity",
            "Private solver repositories (additional auth needed)",
        ],
    },
    {
        "pr": 1129,
        "issue": 842,
        "title": "fix: gitt miner check exits 1 when no valid PAT (#842)",
        "root_cause": (
            "`check.py` iterates over configured validators and displays PAT status, but exits "
            "with code 0 even when `valid_count == 0` (all PATs are invalid). Downstream scripts "
            "rely on exit codes to detect failure, so they miss the all-PATs-invalid state."
        ),
        "impact": (
            "Automation scripts that call `gitt miner check` don't detect when all PATs are invalid. "
            "Miners remain unaware their setup has no valid tokens."
        ),
        "solution": [
            "Return exit code 1 when valid_count == 0 in check.py",
            "Add exit code documentation to CLI help",
        ],
        "tests": [
            "ruff check",
            "ruff format --check",
            "pyright",
            "pytest tests/cli/test_check_exit_code.py -q",
        ],
        "live_verif": [
            "`gitt miner check --network finney; echo Exit: $?` shows 1 when no valid PATs",
        ],
        "how_to_verify": [
            "Set up a config with invalid PATs (or no PATs)",
            "Run `gitt miner check --network finney; echo $?`",
            "Verify exit code is 1 (not 0)",
        ],
        "post_merge": [
            "Confirm exit code 1 when valid_count==0 before next scoring cycle",
            "Update any CI scripts that call gitt miner check to handle exit 1",
        ],
        "edge_cases": [
            "Empty validators list (no validators configured at all) — should also exit 1",
            "Mix of valid and invalid PATs — should exit 0 (some are valid)",
        ],
    },
    {
        "pr": 1130,
        "issue": 841,
        "title": "fix: gitt miner post exits 1 when all PATs rejected (#841)",
        "root_cause": (
            "`post.py` attempts to submit PR data and counts accepted/rejected. When `accepted_count` "
            "is 0 (all PATs rejected), it exits 0 — same as success. Callers can't distinguish "
            "'all rejected' from 'all accepted' without parsing stdout."
        ),
        "impact": (
            "Miner post scripts don't detect complete PAT rejection. Automated retry logic "
            "doesn't trigger because exit code suggests success."
        ),
        "solution": [
            "Return exit code 1 when accepted_count == 0 in post.py",
            "Print summary of accepted/rejected counts at exit",
        ],
        "tests": [
            "ruff check",
            "ruff format --check",
            "pyright",
            "pytest tests/cli/test_post_exit_code.py -q",
        ],
        "live_verif": [
            "`gitt miner post --network finney; echo Exit: $?` shows 1 when all rejected",
        ],
        "how_to_verify": [
            "Set up config where all PATs are rejected",
            "Run `gitt miner post --network finney; echo $?`",
            "Verify exit code is 1 and rejected count is printed",
        ],
        "post_merge": [
            "Confirm exit code 1 when accepted=0 in production",
            "Add to CI: `gitt miner post || echo 'All PATs rejected — check tokens'`",
        ],
        "edge_cases": [
            "Some accepted, some rejected — exit 0 (partial success)",
            "All accepted — exit 0",
            "Network error vs auth rejection — different exit codes?",
        ],
    },
    {
        "pr": 1132,
        "issue": 1098,
        "title": "refactor: remove stale All-Hands-AI/OpenHands repo entry (#1098)",
        "root_cause": (
            "`master_repositories.json` contains an entry for `All-Hands-AI/OpenHands` which no "
            "longer exists or has been renamed. The scoring code hits 404 when trying to fetch "
            "PRs from this repo, wasting a GraphQL query slot per scoring cycle."
        ),
        "impact": (
            "One GraphQL query slot wasted per cycle on a stale repo. With rate limits, "
            "this reduces the effective PR fetch window."
        ),
        "solution": [
            "Remove `All-Hands-AI/OpenHands` from master_repositories.json",
            "Reduce stale entries to improve query efficiency",
        ],
        "why": "404 repos waste rate-limited GraphQL queries. Regular cleanup improves scoring coverage",
        "tests": [
            "ruff check",
            "ruff format --check",
            "pyright",
            "pytest tests/validator/test_repository_list.py -q",
        ],
        "how_to_verify": [
            "Check `master_repositories.json` no longer contains All-Hands-AI/OpenHands",
            "Run `pytest tests/validator/test_repository_list.py` to validate file",
        ],
        "post_merge": [
            "Verify no 404 warnings in validator logs for this repo",
            "Consider adding automated stale-repo detection",
        ],
        "edge_cases": [
            "Repo was renamed (not deleted) — 302 redirect, not 404",
            "Repo changed visibility from public to private",
        ],
    },
    {
        "pr": 1133,
        "issue": 1004,
        "title": "feat: show GitHub username after PAT validation (#1004)",
        "root_cause": (
            "After `validate_pat_token()` confirms a token is valid, it returns only boolean "
            "success. The miner doesn't know WHICH GitHub account the token belongs to — "
            "important when multiple tokens are configured."
        ),
        "impact": (
            "Miners with multiple PATs can't verify which token maps to which GitHub account. "
            "Debugging token issues requires trial-and-error."
        ),
        "solution": [
            "Fetch and display GitHub username (/user endpoint) after PAT validation",
            "Show `Token for @username: valid` instead of just `Token: valid`",
        ],
        "why": "Miners running multiple accounts need clear token-to-account mapping",
        "tests": [
            "ruff check",
            "ruff format --check",
            "pyright",
            "pytest tests/utils/test_github_api_tools.py::test_pat_validation_shows_username -q",
        ],
        "live_verif": [
            "`gitt miner validate-pat` now shows `Token for @alpurkan17: valid`",
        ],
        "how_to_verify": [
            "Run `gitt miner validate-pat` with a valid PAT",
            "Verify output shows `@your-username` in the output",
        ],
        "post_merge": [
            "Confirm miner output shows correct GitHub usernames",
            "Check multi-token configs show distinct usernames per token",
        ],
        "edge_cases": [
            "Token belongs to an organization (not a user) — handle gracefully",
            "Rate-limited /user endpoint — show fallback message",
            "Token with insufficient scope for /user — show partial info",
        ],
    },
]

COMMANDS = {
    "owner": "entrius",
    "repo": "gittensor",
}


def dry_run_body(pr_info: dict) -> str:
    """Generate body for a single PR."""
    return build(
        issue=pr_info["issue"],
        title=pr_info["title"],
        root_cause=pr_info.get("root_cause", ""),
        impact=pr_info.get("impact", ""),
        solution=pr_info.get("solution", []),
        why=pr_info.get("why", ""),
        tests=pr_info.get("tests", ["ruff check"]),
        live_verif=pr_info.get("live_verif", None),
        how_to_verify=pr_info.get("how_to_verify", None),
        post_merge=pr_info.get("post_merge", None),
        edge_cases=pr_info.get("edge_cases", None),
    )


def main():
    ap = argparse.ArgumentParser(description="Upgrade all PR bodies to v3")
    ap.add_argument("--dry-run", action="store_true", help="Preview only")
    ap.add_argument("--pr", type=int, default=None, help="Single PR to upgrade")
    args = ap.parse_args()

    if args.pr:
        targets = [p for p in PRS if p["pr"] == args.pr]
        if not targets:
            print(f"No PR config found for #{args.pr}")
            sys.exit(1)
    else:
        targets = PRS

    print(f"\nUpgrading {len(targets)} PR(s) to v3 body style...\n")

    for pr_info in targets:
        pr_num = pr_info["pr"]
        body = dry_run_body(pr_info)

        print(f"{'='*60}")
        print(f"PR #{pr_num} — {pr_info['issue']}: {pr_info['title'][:60]}")
        print(f"{'='*60}")
        print(f"Body preview:\n{body[:500]}...\n")
        if not args.dry_run:
            update_pr_body(pr_num, body)
        print()

    if args.dry_run:
        print("DRY RUN — no changes made.")
    else:
        print("Done. All PR bodies updated.")


if __name__ == "__main__":
    main()
