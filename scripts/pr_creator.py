#!/usr/bin/env python3
"""
PR Creator — End-to-end PR pipeline.
Workflow: branch → validate → commit → push → create PR.

Usage:
  python3 scripts/pr_creator.py --issue 123 --prefix fix --title "fix: desc (#123)" \\
    --root-cause "reason" --impact "effect" --solution "change1" "change2" \\
    --tests "ruff check" "pytest tests/ -q"
  python3 scripts/pr_creator.py --quick --issue 123    # Interactive mode
  python3 scripts/pr_creator.py --status                # Check open PR threshold
"""

import argparse, subprocess, sys, os, json, textwrap, re
from datetime import datetime, timezone

REPO = "entrius/gittensor"
FORK = "alpurkan17/gittensor"
BASE = "test"
VALID_PREFIXES = ["fix", "feat", "refactor", "perf", "cli", "test", "style", "docs"]

def run(cmd: list, timeout=30, check=True) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            print(f"  [WARN] Exit {r.returncode}: {r.stderr[:200]}")
        return r
    except subprocess.TimeoutExpired:
        print(f"  [ERR] Timeout ({timeout}s)")
        sys.exit(1)

def check_threshold() -> dict:
    """Check open PR threshold: min(10 + floor(ts/300), 30)."""
    r = run(["gh", "pr", "list", "--repo", REPO, "--state", "open",
             "--author", "alpurkan17", "--json", "number", "-L", "30"], timeout=15)
    open_prs = json.loads(r.stdout) if r.stdout.strip() else []
    count = len(open_prs)

    # Try to get token_score (simplified: use open PR count as proxy)
    token_score = 0
    max_prs = min(10 + token_score // 300, 30)
    return {"open": count, "max": max_prs, "available": max_prs - count, "prs": open_prs}

def create_branch(issue: int, prefix: str) -> str:
    """Create a branch from test with proper naming: {prefix}/{issue}-short-desc."""
    branch = f"{prefix}/{issue}"

    # Check if branch already exists
    r = run(["git", "rev-parse", "--verify", branch], timeout=5, check=False)
    if r.returncode == 0:
        print(f"  Branch '{branch}' already exists, switching...")
        run(["git", "checkout", branch], timeout=5)
        return branch

    # Create from test
    print(f"  Creating branch '{branch}' from '{BASE}'...")
    run(["git", "checkout", BASE], timeout=5)
    run(["git", "pull", "origin", BASE], timeout=15)
    run(["git", "checkout", "-b", branch], timeout=5)
    print(f"  Branch '{branch}' created")
    return branch

def validate():
    """Run pre-submit validation."""
    checks = [
        ("ruff check", ["ruff", "check", "."]),
        ("ruff format", ["ruff", "format", "--check", "."]),
        ("pyright (warn only)", ["pyright"]),
    ]
    all_ok = True
    for name, cmd in checks:
        print(f"  [{name}] ", end="", flush=True)
        r = run(cmd, timeout=60, check=False)
        ok = r.returncode == 0
        print(f"{'PASS' if ok else 'FAIL'}")
        if not ok and name != "pyright (warn only)":
            all_ok = False
    if not all_ok:
        print("  Fix lint errors before continuing.")
        sys.exit(1)
    return True

def commit(prefix: str, message: str, issue: int):
    """Stage all and commit with semantic prefix."""
    run(["git", "add", "-A"], timeout=10)

    r = run(["git", "diff", "--cached", "--quiet"], timeout=5, check=False)
    if r.returncode == 0:
        print("  Nothing to commit.")
        return False

    msg = f"{prefix}: {message} (#{issue})"
    r = run(["git", "commit", "-m", msg], timeout=10, check=False)
    if r.returncode != 0:
        print(f"  Commit failed: {r.stderr[:200]}")
        return False
    print(f"  Committed: {msg}")
    return True

def push(branch: str):
    """Push branch to fork origin."""
    print(f"  Pushing to origin/{branch}...")
    r = run(["git", "push", "-u", "origin", branch], timeout=30, check=False)
    if r.returncode != 0:
        # Try with explicit force
        print(f"  Push failed: {r.stderr[:200]}")
        return False
    print(f"  Pushed origin/{branch}")
    return True

def create_pr(issue: int, title: str, body: str, branch: str):
    """Create PR on upstream repo."""
    print(f"  Creating PR: {title}")

    # Write body to temp file to avoid shell escaping issues
    body_file = "/tmp/pr_body.txt"
    with open(body_file, "w") as f:
        f.write(body)

    r = run([
        "gh", "pr", "create",
        "--repo", REPO,
        "--base", BASE,
        "--head", f"{FORK.split('/')[0]}:{branch}",
        "--title", title,
        "--body-file", body_file,
    ], timeout=30, check=False)

    if r.returncode != 0:
        # Try with --body as fallback
        r = run([
            "gh", "pr", "create",
            "--repo", REPO,
            "--base", BASE,
            "--head", f"{FORK.split('/')[0]}:{branch}",
            "--title", title,
            "--body", body,
        ], timeout=30, check=False)

    if r.returncode == 0:
        url = r.stdout.strip()
        pr_num_match = re.search(r'#(\d+)', url)
        pr_num = pr_num_match.group(1) if pr_num_match else "?"
        print(f"  PR #{pr_num}: {url}")

        # Try to add reviewers (may fail on fork)
        add_reviewers(pr_num, url)
        return pr_num, url
    else:
        print(f"  PR creation failed: {r.stderr[:300]}")
        # Try to extract PR number from error
        err = r.stderr
        m = re.search(r'pull/(\d+)', err)
        if m:
            print(f"  PR might exist at #{m.group(1)}")
            return m.group(1), ""
        return None, ""

def add_reviewers(pr_num, url):
    """Add reviewers (may fail on fork — expected)."""
    for reviewer in ["anderdc", "landyndev"]:
        r = run([
            "gh", "api",
            f"repos/{REPO}/pulls/{pr_num}/requested_reviewers",
            "--method", "POST",
            "-f", f"reviewers[]={reviewer}",
        ], timeout=15, check=False)
        if r.returncode == 0:
            print(f"  Reviewer @{reviewer} requested")
        else:
            print(f"  Note: can't add reviewer @{reviewer} from fork (expected)")

def auto_detect_changes() -> dict:
    """Auto-detect changed files to classify the PR."""
    r = run(["git", "diff", "--stat", f"{BASE}..."], timeout=10, check=False)
    diff_stat = r.stdout.strip()

    r2 = run(["git", "diff", "--name-only", f"{BASE}..."], timeout=10, check=False)
    files = r2.stdout.strip().split("\n") if r2.stdout.strip() else []

    has_cli = any("cli" in f or "miner_command" in f for f in files)
    has_test = any("test_" in f for f in files)

    return {
        "diff_stat": diff_stat,
        "files": files,
        "has_cli": has_cli,
        "has_test": has_test,
    }

def run_interactive():
    """Interactive PR creation wizard."""
    from pr_body_builder import build

    print()
    print("=" * 60)
    print("  PR CREATOR — Interactive Wizard")
    print("=" * 60)

    # Check threshold
    threshold = check_threshold()
    print(f"\n  Open PRs: {threshold['open']}/{threshold['max']} (available: {threshold['available']})")
    if threshold['available'] <= 0:
        print("  Threshold full! Wait for a PR to be merged/closed.")
        sys.exit(1)

    issue = int(input("\n  Issue #: ").strip())

    # Get issue details
    r = run(["gh", "issue", "view", str(issue), "--repo", REPO,
             "--json", "title,author,labels,body,state"],
            timeout=15, check=False)
    if r.returncode == 0:
        issue_data = json.loads(r.stdout)
        print(f"  Issue: {issue_data['title']}")
        print(f"  Author: @{issue_data['author']['login']}")
        print(f"  Labels: {[l['name'] for l in issue_data.get('labels', [])]}")
        is_maintainer = issue_data['author']['login'] in ["anderdc", "landyndev"]
        if is_maintainer:
            print(f"  *** Maintainer issue! 1.66x bonus! ***")
    else:
        print(f"  Warning: could not fetch issue #{issue}")

    prefix = input("  Prefix (fix/feat/perf/refactor/cli) [fix]: ").strip() or "fix"
    while prefix not in VALID_PREFIXES:
        prefix = input(f"  Invalid. Choose from: {', '.join(VALID_PREFIXES)} [fix]: ").strip() or "fix"

    desc = input("  Short description (for branch/commit): ").strip()
    while not desc:
        desc = input("  Required: ").strip()

    title = f"{prefix}: {desc} (#{issue})"
    print(f"  Title: {title}")

    root_cause = input("\n  Root cause (Enter to skip): ").strip()
    impact = input("  Impact (Enter to skip): ").strip()

    print("  Solutions (one per line, empty line to finish):")
    solutions = []
    while True:
        line = input("    > ").strip()
        if not line:
            break
        solutions.append(line)
    if not solutions:
        solutions = [desc]

    test_items = []
    print("  Extra tests (one per line, empty=default):")
    while True:
        line = input("    > ").strip()
        if not line:
            break
        test_items.append(line)
    if not test_items:
        test_items = ["ruff check", "ruff format --check", "pyright"]

    has_cli = input("  CLI change? (y/N): ").strip().lower() == "y"

    print("\n  Generating body...")
    body = build(
        issue=issue, title=title,
        root_cause=root_cause, impact=impact,
        solution=solutions, tests=test_items,
        live_verif=["Terminal evidence terlampir"] if has_cli else [],
    )

    print(f"\n  {'='*58}")
    print(body[:800])
    print(f"  {'='*58}")

    ok = input("\n  Create PR? (y/N): ").strip().lower()
    if ok != "y":
        print("  Cancelled.")
        sys.exit(0)

    # Execute pipeline
    print("\n  1. Creating branch...")
    branch = create_branch(issue, prefix)

    print("\n  2. Validating...")
    validate()

    print("\n  3. Committing...")
    committed = commit(prefix, desc, issue)
    if not committed:
        print("  No changes to commit. Still pushing existing state...")

    print("\n  4. Pushing...")
    push(branch)

    print("\n  5. Creating PR...")
    pr_num, url = create_pr(issue, title, body, branch)

    if pr_num:
        print(f"\n  PR #{pr_num} created!")
        if has_cli:
            print(f"\n  Don't forget: capture terminal evidence:")
            print(f"    python3 scripts/capture_terminal.py --pr {pr_num} --before \"<old cmd>\" --after \"<new cmd>\"")
    else:
        print("\n  PR creation failed. Try manually.")

def main():
    p = argparse.ArgumentParser(description="PR Creator — end-to-end pipeline")
    p.add_argument("--issue", type=int, help="Issue number")
    p.add_argument("--prefix", default="fix", choices=VALID_PREFIXES, help="Commit/PR prefix")
    p.add_argument("--title", help="PR title (default: --prefix desc (#issue)")
    p.add_argument("--desc", help="Short description for branch/commit name")
    p.add_argument("--root-cause", default="", help="Root cause of the bug")
    p.add_argument("--impact", default="", help="Impact of the bug")
    p.add_argument("--solution", nargs="+", help="Solutions implemented")
    p.add_argument("--tests", nargs="+", default=["ruff check"], help="Test plan")
    p.add_argument("--why", default="", help="Broader context")
    p.add_argument("--live-verif", nargs="*", default=[], help="Live verification")
    p.add_argument("--quick", action="store_true", help="Interactive mode")
    p.add_argument("--status", action="store_true", help="Check open PR threshold")
    args = p.parse_args()

    if args.status:
        t = check_threshold()
        print(f"\n  Open PRs: {t['open']}/{t['max']}")
        print(f"  Available slots: {t['available']}")
        return

    if args.quick or not any([args.issue, args.title, args.solution]):
        run_interactive()
        return

    # Non-interactive mode
    from pr_body_builder import build

    if not args.issue or not args.solution:
        p.print_help()
        sys.exit(1)

    prefix = args.prefix
    desc = args.desc or args.solution[0][:50]
    title = args.title or f"{prefix}: {desc} (#{args.issue})"

    threshold = check_threshold()
    if threshold['available'] <= 0:
        print(f"\n  Threshold full ({threshold['open']}/{threshold['max']}). Cannot create PR.")
        sys.exit(1)

    body = build(
        issue=args.issue, title=title,
        root_cause=args.root_cause, impact=args.impact,
        solution=args.solution, tests=args.tests,
        why=args.why, live_verif=args.live_verif if args.live_verif else None,
    )

    branch = create_branch(args.issue, prefix)
    validate()
    commit(prefix, desc, args.issue)
    push(branch)
    pr_num, url = create_pr(args.issue, title, body, branch)


if __name__ == "__main__":
    main()
