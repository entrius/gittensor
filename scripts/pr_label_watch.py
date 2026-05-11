#!/usr/bin/env python3
"""
PR Label Watch — Monitor PR labels and alert on mismatches.

Bot xiao-xiao-mao auto-labels based on title prefix, but sometimes it
gets it wrong (e.g. PR #1144: fix: → refactor).  This tool catches
those cases so you can close-recreate before decay kicks in.

Mappings (title prefix → expected label):
  fix:         → bug           (1.1x)
  feat:        → enhancement   (1.25x)
  perf:        → enhancement   (1.25x)
  cli:         → enhancement   (1.25x)
  refactor:    → refactor      (0.25x)
  chore:       → refactor      (0.25x)
  docs:        → documentation (0.5x)
  test:        → documentation (0.5x)

Usage:
  python3 pr_label_watch.py                          # Check all open PRs
  python3 pr_label_watch.py --pr 1144                # Check specific PR
  python3 pr_label_watch.py --json                   # Machine-readable
  python3 pr_label_watch.py --watch                  # Exit 1 if mismatch (cron)
"""

import argparse
import json
import re
import subprocess
import sys

REPO = "entrius/gittensor"

PREFIX_LABEL_MAP = {
    "fix:": "bug",
    "hotfix:": "bug",
    "bugfix:": "bug",
    "feat:": "enhancement",
    "feature:": "enhancement",
    "perf:": "enhancement",
    "cli:": "enhancement",
    "refactor:": "refactor",
    "chore:": "refactor",
    "docs:": "documentation",
    "doc:": "documentation",
    "test:": "documentation",
    "tests:": "documentation",
    "style:": "refactor",
}

LABEL_MULTIPLIERS = {
    "bug": 1.1,
    "enhancement": 1.25,
    "feature": 1.5,
    "refactor": 0.25,
    "documentation": 0.5,
    "drift": 1.0,
}

BOT_NAME = "xiao-xiao-mao[bot]"


def run_gh(args, timeout=15):
    cmd = ["gh"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return json.loads(r.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return None


def get_open_prs(author=None):
    args = ["pr", "list", "--repo", REPO, "--state", "open", "--json", "number,title,labels,headRefName,createdAt"]
    if author:
        args += ["--author", author]
    return run_gh(args) or []


def get_label_events(pr_num):
    events = run_gh(["api", f"repos/{REPO}/issues/{pr_num}/events", "--jq", "[.[] | select(.event == \"labeled\") | {label: .label.name, actor: .actor.login}]"])
    return events or []


def detect_prefix(title):
    for prefix in sorted(PREFIX_LABEL_MAP.keys(), key=len, reverse=True):
        if title.lower().startswith(prefix):
            return prefix, PREFIX_LABEL_MAP[prefix]
    return None, None


def check_pr(pr, json_output=False):
    num = pr["number"]
    title = pr.get("title", "")
    labels = [l["name"] for l in pr.get("labels", [])]
    current_label = labels[-1] if labels else None
    created = pr.get("createdAt", "")

    prefix, expected_label = detect_prefix(title)
    if not prefix:
        return {"pr": num, "title": title, "status": "unknown-prefix"}

    events = get_label_events(num)
    bot_label = None
    for e in events:
        if e.get("actor") == BOT_NAME:
            bot_label = e.get("label")

    mismatch = current_label != expected_label
    mult_current = LABEL_MULTIPLIERS.get(current_label, 1.0)
    mult_expected = LABEL_MULTIPLIERS.get(expected_label, 1.0)
    score_loss = round((mult_expected - mult_current) / mult_expected * 100, 1) if mult_expected > 0 else 0

    result = {
        "pr": num,
        "title": title,
        "prefix": prefix,
        "expected_label": expected_label,
        "current_label": current_label,
        "bot_label": bot_label,
        "mismatch": mismatch,
        "mult_current": mult_current,
        "mult_expected": mult_expected,
        "score_loss_pct": score_loss if mismatch else 0,
        "created": created,
        "severity": "CRITICAL" if mismatch and mult_current < 0.5 else "WARN" if mismatch else "OK",
    }

    if json_output:
        return result

    status = "✅" if not mismatch else "❌"
    print(f"  #{num:<5} {status} [{current_label or 'none':<14}] → expected {expected_label:<14}  {title[:55]}")
    if mismatch:
        print(f"         Bot: {bot_label or 'N/A'} | Score loss: {score_loss}% | Multiplier: {mult_current} → {mult_expected}")
    return result


def main():
    ap = argparse.ArgumentParser(description="PR Label Watch")
    ap.add_argument("--pr", type=int, help="Check specific PR")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--watch", action="store_true", help="Exit 1 if any mismatch")
    ap.add_argument("--author", default="alpurkan17", help="Filter by author")
    args = ap.parse_args()

    if args.pr:
        pr_data = run_gh(["pr", "view", str(args.pr), "--repo", REPO, "--json", "number,title,labels,headRefName,createdAt"])
        if not pr_data:
            print(f"PR #{args.pr} not found")
            sys.exit(1)
        result = check_pr(pr_data, json_output=args.json)
        if args.json:
            print(json.dumps(result, indent=2))
        if args.watch and result.get("mismatch"):
            sys.exit(1)
        return

    prs = get_open_prs(author=args.author)
    if not prs:
        print("No open PRs found.")
        return

    print(f"\n{'='*60}")
    print(f"  PR LABEL WATCH — {REPO} ({args.author})")
    print(f"{'='*60}")
    print(f"  {'#':<6} {'Status':<8} {'Label':<16} {'Expected':<16} {'Title'}")
    print(f"  {'-'*6} {'-'*8} {'-'*16} {'-'*16} {'-'*30}")

    mismatches = 0
    results = []
    for pr in sorted(prs, key=lambda p: p["number"], reverse=True):
        r = check_pr(pr, json_output=args.json)
        results.append(r)
        if r.get("mismatch"):
            mismatches += 1

    print(f"\n  Summary: {len(prs)} PRs, {mismatches} mismatches")

    if args.json:
        print(json.dumps(results, indent=2))

    if args.watch and mismatches > 0:
        print("  ❌ Watch mode: mismatches found")
        sys.exit(1)


if __name__ == "__main__":
    main()
