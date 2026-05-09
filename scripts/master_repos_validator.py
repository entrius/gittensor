#!/usr/bin/env python3
"""
Master Repos Validator — Check master_repositories.json for stale/deleted repos.

Mirror PR #1132 concept: stale repos waste GraphQL query slots.
This tool validates all 215 entries and reports issues.

Usage:
  python3 scripts/master_repos_validator.py                    # Full check
  python3 scripts/master_repos_validator.py --quick            # Fast check (top 30)
  python3 scripts/master_repos_validator.py --fix-stale        # Generate fixed JSON
  python3 scripts/master_repos_validator.py --report           # Generate report only
"""

import argparse, json, subprocess, sys, os, time
from datetime import datetime

REPOS_FILE = "gittensor/validator/weights/master_repositories.json"
STALE_LOG = "stale_repos_found.json"

def load_repos():
    alt_paths = [
        REPOS_FILE,
        "/root/gittensor/gittensor/validator/weights/master_repositories.json",
        "../gittensor/validator/weights/master_repositories.json",
    ]
    for p in alt_paths:
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f), p
    print(f"  Could not find master_repositories.json")
    sys.exit(1)

def check_repo(repo_full: str) -> dict:
    """Check if a repo exists and get basic info."""
    start = time.time()
    # Use gh api with jq-like query to get just what we need
    r = subprocess.run(
        ["gh", "api", f"repos/{repo_full}",
         "--jq", "{name, full_name, archived, disabled, visibility, open_issues_count}"],
        capture_output=True, text=True, timeout=15
    )
    elapsed = time.time() - start

    parsed = {}
    exists = False
    if r.returncode == 0 and r.stdout.strip():
        try:
            parsed = json.loads(r.stdout)
            exists = True
        except:
            pass

    result = {
        "repo": repo_full,
        "exists": exists,
        "status_code": "OK" if exists else f"EXIT_{r.returncode}",
        "latency": round(elapsed, 2),
        "http_status": "OK" if exists else (r.stderr[:100] if r.stderr else "unknown"),
    }

    if parsed:
        result.update({
            "archived": parsed.get("archived", False),
            "disabled": parsed.get("disabled", False),
            "visibility": parsed.get("visibility", "unknown"),
            "open_issues": parsed.get("open_issues_count", 0),
        })

    return result

def validate_all(repos: dict, quick: bool = False):
    """Validate all repos in the list."""
    sorted_repos = sorted(repos.items(), key=lambda x: x[1].get("weight", 0), reverse=True)
    limit = 30 if quick else len(sorted_repos)

    print(f"\n  Validating {limit}/{len(sorted_repos)} repos...")
    print(f"  {'='*60}")

    issues = []
    checked = 0
    errors = 0

    for name, cfg in sorted_repos[:limit]:
        checked += 1
        result = check_repo(name)

        status_icon = "✅" if result["exists"] else "❌"
        archived = " [ARCHIVED]" if result.get("archived") else ""
        disabled = " [DISABLED]" if result.get("disabled") else ""

        if not result["exists"]:
            errors += 1
            issues.append(result)
            print(f"  {status_icon} {name:<45} weight={cfg.get('weight',0):.2f}{archived}{disabled}")
        elif result.get("archived") or result.get("disabled"):
            errors += 1
            issues.append(result)
            print(f"  {status_icon} {name:<45} weight={cfg.get('weight',0):.2f}{archived}{disabled}")

        # Progress indicator
        if not quick and checked % 20 == 0:
            print(f"  ... {checked}/{limit} checked ({errors} issues)")

    # Summary
    print(f"\n  {'='*60}")
    print(f"  Checked: {checked}/{len(sorted_repos)}")
    print(f"  Issues:  {errors}")
    if issues:
        print(f"\n  Stale repos:")
        for issue in issues:
            extra = ""
            if issue.get("archived"): extra += " [ARCHIVED]"
            if issue.get("disabled"): extra += " [DISABLED]"
            print(f"    ❌ {issue['repo']}{extra}")
    else:
        print(f"  ✅ No stale repos found!")

    # Save issues
    if issues:
        with open(STALE_LOG, "w") as f:
            json.dump(issues, f, indent=2)
        print(f"  Saved to {STALE_LOG}")

    return issues

def fix_stale(repos: dict) -> dict:
    """Remove stale repos and generate clean JSON."""
    print(f"\n  Checking ALL {len(repos)} repos for stale entries...")
    dead = []

    for i, (name, cfg) in enumerate(repos.items()):
        result = check_repo(name)
        if not result["exists"] or result.get("archived") or result.get("disabled"):
            dead.append(name)
            print(f"  ❌ {name} — removing")

        if (i + 1) % 30 == 0:
            print(f"  ... {i+1}/{len(repos)} checked ({len(dead)} stale)")

    if not dead:
        print(f"  ✅ No stale repos found — no changes needed.")
        return repos

    clean = {k: v for k, v in repos.items() if k not in dead}
    output_file = "master_repositories_clean.json"
    with open(output_file, "w") as f:
        json.dump(clean, f, indent=2)

    print(f"\n  Removed {len(dead)} stale repos:")
    for r in dead:
        print(f"    - {r}")
    print(f"  Clean list saved to {output_file}")
    print(f"  Review and replace: cp {output_file} {REPOS_FILE}")
    return clean

def generate_report(repos: dict):
    """Generate a report of repo health."""
    print(f"\n  REPO HEALTH REPORT")
    print(f"  {'='*60}")
    print(f"  Total: {len(repos)} repos")

    # Weight distribution
    weights = [c.get("weight", 0) for c in repos.values()]
    tiers = {"1.0": 0, "0.5-0.99": 0, "0.2-0.49": 0, "0.1-0.19": 0, "<0.1": 0}
    for w in weights:
        if w == 1.0: tiers["1.0"] += 1
        elif w >= 0.5: tiers["0.5-0.99"] += 1
        elif w >= 0.2: tiers["0.2-0.49"] += 1
        elif w >= 0.1: tiers["0.1-0.19"] += 1
        else: tiers["<0.1"] += 1

    for tier, count in tiers.items():
        bar = "█" * count if count < 50 else "█" * 50 + f" ({count})"
        print(f"  {tier:<12} {bar}")

    print(f"\n  Recommendations:")
    print(f"  - Focus PRs on repos with weight ≥ 0.5 (mirror_enabled=true)")
    print(f"  - Avoid repos with weight < 0.2 (negligible score impact)")
    print(f"  - Run --validate monthly to catch stale repos early")

def main():
    p = argparse.ArgumentParser(description="Master Repos Validator")
    p.add_argument("--quick", action="store_true", help="Quick check (top 30)")
    p.add_argument("--fix-stale", action="store_true", help="Remove stale repos & generate clean JSON")
    p.add_argument("--report", action="store_true", help="Generate health report")
    args = p.parse_args()

    repos, filepath = load_repos()
    print(f"  Loaded {len(repos)} repos from {filepath}")

    if args.fix_stale:
        fix_stale(repos)
    elif args.report:
        generate_report(repos)
    else:
        validate_all(repos, quick=args.quick)

if __name__ == "__main__":
    main()
