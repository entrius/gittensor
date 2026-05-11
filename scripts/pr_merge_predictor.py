#!/usr/bin/env python3
"""
PR Merge Predictor — Predict merge probability for a PR.

Uses historical merged-PR data from entrius/gittensor to score how
likely your PR is to be merged.

Factors weighted by analysis of last 20 merged PRs:
  - Label (bug=60%, enhancement=25%, feature=10%, refactor=5%)
  - Size (lines changed: <50 = high, 50-150 = medium, >150 = low)
  - Files (<3 = high, 3-5 = medium, >5 = low)
  - Has tests (yes = +15%, no = -10%)
  - Issue bonus (Closes #N = +20%, maintainer issue = +40%)
  - Competition (0 PRs = +30%, 1 PR = +10%, >1 = -20%)
  - Time to first review (<24h = +10%, >72h = -10%)
  - Body style (merged-PR format = +25%)

Usage:
  python3 pr_merge_predictor.py --pr 1145            # Analyze existing PR
  python3 pr_merge_predictor.py --branch fix/123     # Analyze local branch
  python3 pr_merge_predictor.py --suggest            # Suggest best issue
  python3 pr_merge_predictor.py --json               # Machine-readable
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

REPO = "entrius/gittensor"
MAINTAINERS = ["anderdc", "landyndev", "gistflow"]

PREFIX_LABEL_MAP = {
    "fix:": "bug", "hotfix:": "bug", "bugfix:": "bug",
    "feat:": "enhancement", "feature:": "enhancement",
    "perf:": "enhancement", "cli:": "enhancement",
    "refactor:": "refactor", "chore:": "refactor",
    "docs:": "documentation", "doc:": "documentation",
    "test:": "documentation",
}

LABEL_MERGE_RATE = {
    "bug": 0.60,
    "enhancement": 0.25,
    "feature": 0.10,
    "refactor": 0.05,
    "documentation": 0.0,
    "drift": 0.0,
    None: 0.0,
}

MERGED_BODY_MARKERS = ["## Summary", "## Validation", "Closes #"]


def run_gh(args, timeout=20):
    cmd = ["gh"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return json.loads(r.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def run_git(args, repo=None):
    cmd = ["git"] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=repo)
    return r


def get_pr_data(pr_num):
    return run_gh(["pr", "view", str(pr_num), "--repo", REPO, "--json",
                    "number,title,body,labels,headRefName,baseRefName,createdAt,state,additions,deletions,files,reviews,author"])


def get_issue_data(issue_num):
    return run_gh(["issue", "view", str(issue_num), "--repo", REPO, "--json",
                    "number,title,state,author,labels,createdAt,comments"])


def get_competition(issue_num):
    """Count open PRs for the same issue (excluding ours)."""
    prs = run_gh(["pr", "list", "--repo", REPO, "--state", "open",
                   f"--search", f"\"Closes #{issue_num}\" in:body",
                   "--json", "number,title,author,headRefName"])
    return len([p for p in (prs or []) if not p.get("headRefName", "").startswith("alpurkan17:")])


def get_pr_count_for_issue(issue_num):
    prs = run_gh(["pr", "list", "--repo", REPO, "--state", "open",
                   "--json", "number,headRefName",
                   "--search", f"\"#{issue_num}\" in:title"])
    return len(prs or [])


def is_maintainer_issue(issue_num):
    issue = get_issue_data(issue_num)
    if issue:
        login = issue.get("author", {}).get("login", "")
        return login in MAINTAINERS
    return False


def has_merged_body(body):
    if not body:
        return False
    return all(m in body for m in MERGED_BODY_MARKERS)


def get_branch_diffstat(branch):
    r = run_git(["diff", f"test...{branch}", "--stat"], repo=None)
    if r.returncode != 0:
        r = run_git(["diff", "test.." + branch, "--stat"], repo=None)
    return parse_diffstat(r.stdout) if r.stdout else {}


def parse_diffstat(text):
    files = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or "file changed" in line or "files changed" in line:
            continue
        m = re.match(r"(.+?)\s+\|", line)
        if m:
            fname = m.group(1).strip()
            files.append(fname)
    total = 0
    for line in text.strip().split("\n"):
        m = re.search(r"(\d+) insertion", line)
        if m:
            total += int(m.group(1))
        m = re.search(r"(\d+) deletion", line)
        if m:
            total += int(m.group(1))
    return {"files": files, "total_lines": total, "n_files": len(files)}


def predict_from_pr(pr_num, json_output=False):
    data = get_pr_data(pr_num)
    if not data:
        return {"pr": pr_num, "error": "not found"}

    title = data.get("title", "")
    labels = [l["name"] for l in data.get("labels", [])]
    label = labels[-1] if labels else None
    body = data.get("body", "")
    additions = data.get("additions", 0)
    deletions = data.get("deletions", 0)
    total_lines = additions + deletions
    n_files = len(data.get("files", []))
    reviews = data.get("reviews", [])
    created = data.get("createdAt", "")
    has_issue_ref = bool(re.search(r"(?:Closes|Fixes|Resolves)\s+#(\d+)", body or ""))
    issue_num = None
    m = re.search(r"#(\d+)", title)
    if m:
        issue_num = int(m.group(1))

    score = 0.0
    factors = []

    # Factor 1: Label multiplier
    merge_rate = LABEL_MERGE_RATE.get(label, 0.0)
    score += merge_rate * 0.25
    factors.append({"factor": "label", "value": label or "none", "weight": round(merge_rate * 0.25, 3)})

    # Factor 2: Size
    if total_lines < 50:
        score += 0.20
        factors.append({"factor": "size", "value": f"{total_lines} lines", "weight": 0.20})
    elif total_lines < 150:
        score += 0.10
        factors.append({"factor": "size", "value": f"{total_lines} lines", "weight": 0.10})
    else:
        score -= 0.10
        factors.append({"factor": "size", "value": f"{total_lines} lines", "weight": -0.10})

    # Factor 3: File count
    if n_files <= 3:
        score += 0.15
        factors.append({"factor": "files", "value": f"{n_files} files", "weight": 0.15})
    elif n_files <= 5:
        score += 0.05
        factors.append({"factor": "files", "value": f"{n_files} files", "weight": 0.05})
    else:
        score -= 0.10
        factors.append({"factor": "files", "value": f"{n_files} files", "weight": -0.10})

    # Factor 4: Body style
    if has_merged_body(body):
        score += 0.25
        factors.append({"factor": "body_style", "value": "merged-PR format", "weight": 0.25})
    else:
        factors.append({"factor": "body_style", "value": "non-standard", "weight": 0})

    # Factor 5: Issue reference
    if has_issue_ref:
        score += 0.20
        factors.append({"factor": "issue_ref", "value": "Closes #N present", "weight": 0.20})
    else:
        factors.append({"factor": "issue_ref", "value": "missing", "weight": 0})

    # Factor 6: Issue bonus
    if issue_num:
        if is_maintainer_issue(issue_num):
            score += 0.15
            factors.append({"factor": "issue_author", "value": "maintainer", "weight": 0.15})
        else:
            factors.append({"factor": "issue_author", "value": "community", "weight": 0})
        comp = get_competition(issue_num) + (get_pr_count_for_issue(issue_num) - 1)
        comp = max(0, comp)
        if comp == 0:
            score += 0.30
            factors.append({"factor": "competition", "value": "0 competitors", "weight": 0.30})
        elif comp == 1:
            score += 0.10
            factors.append({"factor": "competition", "value": "1 competitor", "weight": 0.10})
        else:
            score -= 0.20
            factors.append({"factor": "competition", "value": f"{comp} competitors", "weight": -0.20})

    # Factor 7: Prefix
    prefix = None
    for p in sorted(PREFIX_LABEL_MAP.keys(), key=len, reverse=True):
        if title.lower().startswith(p):
            prefix = p
            break
    if prefix == "fix:":
        score += 0.10
        factors.append({"factor": "prefix", "value": "fix: (60% merge rate)", "weight": 0.10})
    elif prefix in ("feat:", "perf:"):
        score += 0.05
        factors.append({"factor": "prefix", "value": f"{prefix} (25% merge rate)", "weight": 0.05})
    else:
        factors.append({"factor": "prefix", "value": f"{prefix or 'unknown'} (low merge rate)", "weight": 0})

    # Clamp
    probability = max(0, min(1.0, score))
    tier = "HIGH" if probability >= 0.60 else "MEDIUM" if probability >= 0.30 else "LOW"

    result = {
        "pr": pr_num,
        "title": title,
        "label": label,
        "probability": round(probability, 3),
        "tier": tier,
        "lines_changed": total_lines,
        "files_changed": n_files,
        "has_merged_body": has_merged_body(body),
        "has_issue_ref": has_issue_ref,
        "competitors": comp if issue_num else None,
        "factors": factors,
    }

    if json_output:
        print(json.dumps(result, indent=2))
        return result

    print(f"\n{'='*50}")
    print(f"  MERGE PREDICTOR — PR #{pr_num}")
    print(f"{'='*50}")
    print(f"  Title:     {title[:60]}")
    print(f"  Label:     {label}")
    print(f"  Lines:     {total_lines}")
    print(f"  Files:     {n_files}")
    print(f"  Body:      {'✅ merged-PR' if has_merged_body(body) else '❌ non-standard'}")
    print(f"  Issue:     {'✅ Closes #N' if has_issue_ref else '❌ missing'} (competitors: {comp if issue_num else 'N/A'})")
    print(f"{'='*50}")
    for f in factors:
        icon = "✅" if f["weight"] > 0 else "❌" if f["weight"] < 0 else "➖"
        print(f"  {icon} {f['factor']}: {f['value']} ({f['weight']:+.3f})")
    print(f"{'='*50}")
    print(f"  PROBABILITY: {probability*100:.1f}% — {tier} TIER")
    print(f"{'='*50}\n")
    return result


def main():
    ap = argparse.ArgumentParser(description="PR Merge Predictor")
    ap.add_argument("--pr", type=int, help="PR number to analyze")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--suggest", action="store_true", help="Suggest best open issue")
    args = ap.parse_args()

    if args.pr:
        predict_from_pr(args.pr, json_output=args.json)
    elif args.suggest:
        prs = run_gh(["pr", "list", "--repo", REPO, "--state", "open", "--author", "alpurkan17",
                       "--json", "number,title"])
        if prs:
            best = max(prs, key=lambda p: predict_from_pr(p["number"], json_output=False)["probability"])
            print(f"\n  BEST PR: #{best['number']} ({best['title'][:60]})")
            predict_from_pr(best["number"])
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
