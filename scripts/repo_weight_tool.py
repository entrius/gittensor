#!/usr/bin/env python3
"""
Repo Weight Tool — Analyze repo weights and find high-value PR opportunities.

Repo Weight is scoring factor #1 (0.01–1.0x).
Targeting high-weight repos maximizes PR score.

Usage:
  python3 scripts/repo_weight_tool.py                    # Full report
  python3 scripts/repo_weight_tool.py --top 10           # Top 10 repos
  python3 scripts/repo_weight_tool.py --repos-with-issues # Repos with open issues
  python3 scripts/repo_weight_tool.py --opportunities    # Best PR opportunities
  python3 scripts/repo_weight_tool.py --validate         # Check for stale repos
"""

import argparse, json, subprocess, sys, os, re
from datetime import datetime

REPOS_FILE = "gittensor/validator/weights/master_repositories.json"
MAINTAINERS = ["anderdc", "landyndev", "gistflow"]

def load_repos():
    """Load master_repositories.json."""
    if not os.path.exists(REPOS_FILE):
        # Try alternate paths
        alt_paths = [
            "/root/gittensor/gittensor/validator/weights/master_repositories.json",
            "../gittensor/validator/weights/master_repositories.json",
        ]
        for p in alt_paths:
            if os.path.exists(p):
                with open(p) as f:
                    return json.load(f)
        print(f"  Could not find {REPOS_FILE}")
        sys.exit(1)
    with open(REPOS_FILE) as f:
        return json.load(f)

def run_gh(args: list, timeout=30):
    cmd = ["gh"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout) if r.stdout.strip() else None
    except:
        return None

def check_repo_exists(repo_full: str) -> tuple:
    """Check if a GitHub repo exists. Returns (exists: bool, status_code: int)."""
    r = subprocess.run(
        ["gh", "api", f"repos/{repo_full}", "--method", "HEAD"],
        capture_output=True, text=True, timeout=15
    )
    return r.returncode == 0, r.returncode

def classify_repo(repo_name: str, cfg: dict) -> dict:
    """Classify a repo by its characteristics."""
    weight = cfg.get("weight", 0.01)
    mirror = cfg.get("mirror_enabled", False)
    label_pipeline = cfg.get("trusted_label_pipeline", False)

    name_lower = repo_name.lower()

    # Category
    if "gittensor" in name_lower or "subtensor" in name_lower:
        category = "core"
    elif "allways" in name_lower:
        category = "ecosystem"
    elif any(x in name_lower for x in ["openclaw", "hermes", "bitcoin", "paperclip"]):
        category = "partner"
    else:
        category = "community"

    # PR potential (based on repo activity and weight)
    pr_potential = "high" if weight >= 0.8 else ("medium" if weight >= 0.3 else "low")

    return {
        "weight": weight,
        "mirror": mirror,
        "label_pipeline": label_pipeline,
        "category": category,
        "pr_potential": pr_potential,
    }

def fetch_open_issues(repo: str, limit: int = 20) -> list:
    """Fetch open issues for a repo."""
    r = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "open",
         "--json", "number,title,author,labels,createdAt,url",
         "-L", str(limit)],
        capture_output=True, text=True, timeout=20
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        return json.loads(r.stdout)
    except:
        return []

def display_weight_report(repos: dict, top_n: int = 30):
    """Display the weight report."""
    sorted_repos = sorted(repos.items(), key=lambda x: x[1].get("weight", 0), reverse=True)

    print()
    print(f"  REPO WEIGHT REPORT — {len(sorted_repos)} repos")
    print(f"  {'='*70}")
    print(f"  {'Repo':<40} {'Weight':<8} {'Category':<12} {'PR Potential':<12} {'Mirror':<8}")
    print(f"  {'-'*70}")

    tiers = {"⭐ MAX (1.0)": [], "HIGH (0.5-0.99)": [], "MEDIUM (0.2-0.49)": [], "LOW (<0.2)": []}

    for name, cfg in sorted_repos:
        cls = classify_repo(name, cfg)
        tier_key = (
            "⭐ MAX (1.0)" if cls["weight"] == 1.0
            else "HIGH (0.5-0.99)" if cls["weight"] >= 0.5
            else "MEDIUM (0.2-0.49)" if cls["weight"] >= 0.2
            else "LOW (<0.2)"
        )
        tiers[tier_key].append((name, cls))

    for tier_name, tier_repos in tiers.items():
        if not tier_repos:
            continue
        print(f"\n  [{tier_name}] ({len(tier_repos)} repos)")
        for name, cls in tier_repos[:top_n]:
            mirror_icon = "✅" if cls["mirror"] else " "
            print(f"  {name:<40} {cls['weight']:<8.2f} {cls['category']:<12} {cls['pr_potential']:<12} {mirror_icon:<8}")
        if len(tier_repos) > top_n:
            print(f"  ... and {len(tier_repos) - top_n} more")

    print()
    print(f"  Summary:")
    print(f"    MAX weight (1.0): {len(tiers['⭐ MAX (1.0)'])} repos")
    print(f"    Mirror enabled: {sum(1 for n,c in sorted_repos if c.get('mirror_enabled'))} repos")
    print(f"    Label pipeline: {sum(1 for n,c in sorted_repos if c.get('trusted_label_pipeline'))} repos")
    print()

def find_opportunities(repos: dict, max_repos: int = 5):
    """Find best PR opportunities across high-weight repos."""
    print()
    print(f"  PR OPPORTUNITIES — high-weight repos with open issues")
    print(f"  {'='*70}")

    # Focus on high-weight repos
    high_weight = sorted(
        [(n, c) for n, c in repos.items() if c.get("weight", 0) >= 0.5 and c.get("mirror_enabled", False)],
        key=lambda x: x[1].get("weight", 0), reverse=True
    )

    found_any = False
    for repo_name, cfg in high_weight[:max_repos]:
        print(f"\n  {repo_name} (weight={cfg['weight']})")
        issues = fetch_open_issues(repo_name, 10)
        if not issues:
            print(f"    No open issues found")
            continue

        found_any = True
        for issue in issues[:5]:
            author = issue.get("author", {})
            author_login = author.get("login", "?") if isinstance(author, dict) else "?"
            is_maint = author_login in MAINTAINERS
            labels = [l["name"] for l in issue.get("labels", []) if isinstance(l, dict)]
            bonus = "1.66x!" if is_maint else "1.33x"
            maint_tag = " [MAINTAINER]" if is_maint else ""
            print(f"    #{issue['number']:<5} {issue['title'][:55]}")
            print(f"          @{author_login}{maint_tag} | labels: {labels[:3]} | bonus: {bonus}")

    if not found_any:
        print("  No high-weight repos with open issues found.")

    print()

def validate_repos(repos: dict):
    """Check for stale/non-existent repos in the list."""
    print()
    print(f"  REPO VALIDATION — checking {len(repos)} repos for accessibility")
    print(f"  {'='*70}")

    # Check first 30 repos for responsiveness
    sorted_repos = sorted(repos.items(), key=lambda x: x[1].get("weight", 0), reverse=True)
    dead_repos = []

    for i, (name, cfg) in enumerate(sorted_repos):
        status = "?"
        exists, code = check_repo_exists(name)
        if not exists:
            status = f"❌ HTTP {code}"
            dead_repos.append(name)
            print(f"  {status:<12} {name} (weight={cfg.get('weight', 0)})")
        elif i < 5:  # Only show first 5 alive ones
            status = f"✅ OK"
            # Skip printing OK ones to reduce noise

    if not dead_repos:
        print(f"  ✅ All {len(repos)} repos are accessible!")
    else:
        print(f"\n  ⚠️  Found {len(dead_repos)} stale repos:")
        for r in dead_repos:
            print(f"    ❌ {r}")

    print()

def display_quick_summary(repos: dict):
    """Quick summary of repo weight landscape."""
    weights = [c.get("weight", 0) for c in repos.values()]
    mirror_count = sum(1 for c in repos.values() if c.get("mirror_enabled"))
    label_count = sum(1 for c in repos.values() if c.get("trusted_label_pipeline"))

    print(f"  Repo Weight Landscape:")
    print(f"    Total repos:    {len(repos)}")
    print(f"    Max weight:     {max(weights):.2f}")
    print(f"    Avg weight:     {sum(weights)/len(weights):.3f}")
    print(f"    Mirror enabled: {mirror_count}")
    print(f"    Label pipeline: {label_count}")
    print(f"  Our repos:")
    print(f"    entrius/gittensor  1.00 ✅ (10 PRs)")
    print(f"    entrius/allways    1.00 ✅ (1 PR)")

def main():
    p = argparse.ArgumentParser(description="Repo Weight Tool — analyze repo weights and opportunities")
    p.add_argument("--top", type=int, default=30, help="Show top N repos by weight")
    p.add_argument("--repos-with-issues", action="store_true", help="Show high-weight repos with issues")
    p.add_argument("--opportunities", action="store_true", help="Find best PR opportunities")
    p.add_argument("--validate", action="store_true", help="Check for stale repos")
    p.add_argument("--quick", action="store_true", help="Quick summary")
    args = p.parse_args()

    repos = load_repos()
    if not isinstance(repos, dict):
        print("Error: master_repositories.json must be a dict")
        sys.exit(1)

    if args.validate:
        validate_repos(repos)
    elif args.opportunities:
        find_opportunities(repos)
    elif args.repos_with_issues:
        find_opportunities(repos, max_repos=20)
    elif args.quick:
        display_quick_summary(repos)
    else:
        display_weight_report(repos, args.top)

if __name__ == "__main__":
    main()
