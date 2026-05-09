#!/usr/bin/env python3
"""
Issue Scout — Find and rank issues by scoring potential.

Ranks issues by:
  1. Maintainer author (1.66x issue bonus) vs contributor (1.33x)
  2. Label multiplier (bug=1.1x, enhancement=1.25x, feature=1.5x)
  3. Freshness (newer = less time decay risk)
  4. Unassigned or available
  5. Has clear description / reproduction steps

Usage:
  python3 scripts/issue_scout.py                          # Default: top 15
  python3 scripts/issue_scout.py --repo entrius/allways   # Other repo
  python3 scripts/issue_scout.py --label bug              # Filter by label
  python3 scripts/issue_scout.py --min-score 1.4          # Min combined score
  python3 scripts/issue_scout.py --suggest                # Suggest best issue to work on NOW
  python3 scripts/issue_scout.py --check-merged           # Check what issues were recently fixed
"""

import argparse, json, subprocess, sys, re
from datetime import datetime, timezone

REPO = "entrius/gittensor"
MAINTAINERS = ["anderdc", "landyndev", "gistflow"]

LABEL_MULT = {
    "bug": 1.1,
    "enhancement": 1.25,
    "feature": 1.5,
    "refactor": 0.25,
    "documentation": 0.5,
}

PREFIX_MULT = {
    "fix": "bug",
    "feat": "enhancement",
    "perf": "enhancement",
    "cli": "enhancement",
}

def run_gh(args: list, timeout=30) -> dict | list:
    cmd = ["gh"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return []
        return json.loads(r.stdout) if r.stdout.strip() else []
    except:
        return []

def list_open_issues(repo: str, label: str = None) -> list:
    """Get open issues from the repo."""
    args = ["issue", "list", "--repo", repo, "--state", "open",
            "--json", "number,title,author,labels,createdAt,updatedAt,body,url,assignees",
            "-L", "100"]
    if label:
        args.extend(["--label", label])
    issues = run_gh(args, timeout=20)
    return issues if isinstance(issues, list) else []

def list_merged_issues(repo: str, days: int = 14) -> set:
    """Get recently closed/merged issue numbers."""
    # Search recent merged PRs for Fixes #N
    args = ["pr", "list", "--repo", repo, "--state", "merged",
            "--json", "number,title,body,mergedAt",
            "-L", "50"]
    prs = run_gh(args, timeout=20)
    if not isinstance(prs, list):
        return set()

    merged_issues = set()
    now = datetime.now(timezone.utc)
    for pr in prs:
        merged_at = pr.get("mergedAt", "")
        if merged_at:
            try:
                mdate = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
                delta = now - mdate
                if delta.days > days:
                    continue
            except:
                pass

        body = pr.get("body", "") or ""
        # Extract Fixes/Closes #N
        for m in re.finditer(r'(?:Fixes|Closes|Resolves)\s+#(\d+)', body, re.IGNORECASE):
            merged_issues.add(int(m.group(1)))
    return merged_issues

def score_issue(issue: dict, merged_set: set = None) -> dict:
    """Score an issue by its PR potential."""
    author = issue.get("author", {})
    author_login = author.get("login", "unknown") if isinstance(author, dict) else "unknown"
    labels = issue.get("labels", [])
    label_names = [l["name"] for l in labels if isinstance(l, dict)]
    body = issue.get("body", "") or ""
    created = issue.get("createdAt", "")
    assignees = issue.get("assignees", [])

    # Issue bonus
    is_maintainer = author_login in MAINTAINERS
    issue_bonus = 1.66 if is_maintainer else 1.33

    # Label multiplier (best label wins)
    label_mult = 1.0
    best_label = ""
    for ln in label_names:
        ln_lower = ln.lower()
        if ln_lower in LABEL_MULT:
            m = LABEL_MULT[ln_lower]
            if m > label_mult:
                label_mult = m
                best_label = ln_lower

    # Recommended prefix
    label_to_prefix = {v: k for k, v in PREFIX_MULT.items()}
    recommended_prefix = "fix"
    for label, prefix in [("bug", "fix"), ("enhancement", "feat"),
                          ("feature", "feat"), ("documentation", "docs")]:
        if label == best_label:
            recommended_prefix = prefix

    # Freshness (newer = better, avoid time decay)
    days_old = 999
    if created:
        try:
            cdate = datetime.fromisoformat(created.replace("Z", "+00:00"))
            days_old = (datetime.now(timezone.utc) - cdate).days
        except:
            pass
    freshness = max(0.3, 1.0 - (days_old / 90))  # 90d = 0.3

    # Description quality
    has_repro = bool(re.search(r'(repro|steps|expected|actual|traceback|error)', body, re.IGNORECASE))
    body_len = len(body.strip())
    desc_quality = min(1.0, body_len / 500) + (0.2 if has_repro else 0)

    # Already fixed?
    already_fixed = merged_set and issue["number"] in merged_set

    # Assignee status
    is_assigned = len(assignees) > 0
    assigned_to_me = any(
        a.get("login") == "alpurkan17" for a in assignees
        if isinstance(a, dict)
    ) if assignees else False

    # Combined score
    combined = issue_bonus * label_mult * freshness * (1.0 if not already_fixed else 0.1)

    return {
        "number": issue["number"],
        "title": issue.get("title", ""),
        "url": issue.get("url", ""),
        "author": author_login,
        "is_maintainer": is_maintainer,
        "issue_bonus": issue_bonus,
        "best_label": best_label or "none",
        "label_mult": label_mult,
        "days_old": days_old,
        "freshness": round(freshness, 3),
        "desc_quality": round(desc_quality, 2),
        "has_repro": has_repro,
        "already_fixed": already_fixed,
        "assigned": is_assigned,
        "assigned_to_me": assigned_to_me,
        "combined_score": round(combined, 4),
        "recommended_prefix": recommended_prefix,
        "labels": label_names,
    }

def display_issues(scored: list, max_results: int = 15):
    """Display scored issues in a ranked table."""
    if not scored:
        print("  No issues found.")
        return

    print(f"\n  {'#' if len(scored)>5 else ''}Ranking {len(scored)} issues by PR scoring potential\n")

    header = f"  {'#':<4} {'Issue':<6} {'Score':<8} {'Bonus':<6} {'Label':<14} {'Age':<5} {'Fix':<4} {'Prefix':<8} Title"
    print(header)
    print("  " + "-" * len(header))

    for i, s in enumerate(scored[:max_results], 1):
        label_display = f"{s['best_label']} ({s['label_mult']}x)"
        bonus_display = f"{'M' if s['is_maintainer'] else 'C'} {s['issue_bonus']}x"
        fix_status = "FIXED" if s['already_fixed'] else "OPEN" if not s['assigned'] else ("MINE" if s['assigned_to_me'] else "TAKEN")
        age = f"{s['days_old']}d" if s['days_old'] < 999 else "?"
        print(f"  {i:<4} #{s['number']:<4} {s['combined_score']:<8.3f} {bonus_display:<6} {label_display:<14} {age:<5} {fix_status:<4} {s['recommended_prefix']:<8} {s['title'][:55]}")

    print()

def suggest_best(scored: list):
    """Suggest the best issue to work on NOW."""
    if not scored:
        print("  No issues available.")
        return

    # Filter: not already fixed, not assigned to others
    viable = [s for s in scored if not s['already_fixed'] and not s.get('assigned_to_other', False)]
    if not viable:
        # Relax filter
        viable = scored

    best = viable[0]
    print()
    print(f"  BEST ISSUE TO WORK ON: #{best['number']}")
    print(f"  {'='*50}")
    print(f"  Title:    {best['title']}")
    print(f"  Author:   @{best['author']} ({'MAINTAINER' if best['is_maintainer'] else 'contributor'})")
    print(f"  Bonus:    {best['issue_bonus']}x {'(maintainer 1.66x!)' if best['is_maintainer'] else '(standard 1.33x)'}")
    print(f"  Label:    {best['best_label']} ({best['label_mult']}x)")
    print(f"  Score:    {best['combined_score']}")
    print(f"  Age:      {best['days_old']}d")
    print(f"  Repro:    {'Yes' if best['has_repro'] else 'No'}")
    print()
    print(f"  Create PR with:")
    print(f"    python3 scripts/pr_creator.py --quick --issue {best['number']}")
    print()

def main():
    p = argparse.ArgumentParser(description="Issue Scout — find & rank issues by PR score potential")
    p.add_argument("--repo", default=REPO, help=f"Repository (default: {REPO})")
    p.add_argument("--label", default="", help="Filter by label (bug, enhancement, etc)")
    p.add_argument("--limit", type=int, default=15, help="Max results (default: 15)")
    p.add_argument("--suggest", action="store_true", help="Suggest single best issue to work on")
    p.add_argument("--check-merged", action="store_true", help="Check recently merged issues")
    p.add_argument("--all", action="store_true", help="Show all issues, no limit")
    args = p.parse_args()

    if args.check_merged:
        print(f"\n  Checking recently merged issues in {args.repo}...")
        merged = list_merged_issues(args.repo)
        if merged:
            print(f"  Recently fixed issues ({len(merged)}): {sorted(merged)}")
        else:
            print("  No recently merged issues found.")
        return

    print(f"\n  Scanning open issues in {args.repo}...")
    if args.label:
        print(f"  Filter: label={args.label}")

    issues = list_open_issues(args.repo, args.label if args.label else None)
    if not issues:
        print("  No open issues found.")
        sys.exit(1)

    # Check merged issues to avoid duplicates
    merged_set = list_merged_issues(args.repo)

    scored = []
    for issue in issues:
        s = score_issue(issue, merged_set)
        scored.append(s)

    # Sort by combined score descending
    scored.sort(key=lambda x: x["combined_score"], reverse=True)

    max_results = len(scored) if args.all else args.limit

    if args.suggest:
        suggest_best(scored)
    else:
        display_issues(scored, max_results)

    # Summary
    maintainer_count = sum(1 for s in scored if s['is_maintainer'])
    already_fixed = sum(1 for s in scored if s['already_fixed'])
    available = sum(1 for s in scored if not s['assigned'] and not s['already_fixed'])
    print(f"  Summary: {len(scored)} issues, {maintainer_count} maintainer, {already_fixed} recently fixed, {available} available")
    print()

if __name__ == "__main__":
    main()
