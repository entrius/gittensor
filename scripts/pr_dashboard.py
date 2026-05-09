#!/usr/bin/env python3
"""
PR Dashboard — Quick one-glance status of all PRs + scoring impact.

Usage:
  python3 scripts/pr_dashboard.py                # Default view
  python3 scripts/pr_dashboard.py --watch        # Watch mode (refresh every 30s)
  python3 scripts/pr_dashboard.py --json         # JSON output
  python3 scripts/pr_dashboard.py --action-items # Show only action items
"""

import argparse, json, subprocess, sys, re, os
from datetime import datetime, timezone
from time import sleep

REPO = "entrius/gittensor"

LABEL_MULT = {
    "bug": 1.1,
    "enhancement": 1.25,
    "feature": 1.5,
    "refactor": 0.25,
    "documentation": 0.5,
}

def run_gh(args: list, timeout=30):
    cmd = ["gh"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return []
        return json.loads(r.stdout) if r.stdout.strip() else []
    except:
        return []

def get_prs() -> list:
    """Fetch all open PRs by alpurkan17."""
    return run_gh([
        "pr", "list", "--repo", REPO, "--state", "open",
        "--author", "alpurkan17",
        "--json", "number,title,headRefName,createdAt,labels,reviews,state,url,mergedAt,closedAt",
        "-L", "30"
    ], timeout=15)

def get_closed_prs(days: int = 30) -> list:
    """Fetch recently closed PRs."""
    return run_gh([
        "pr", "list", "--repo", REPO, "--state", "closed",
        "--author", "alpurkan17",
        "--json", "number,title,state,createdAt,closedAt,mergedAt,labels,url",
        "-L", "30"
    ], timeout=15)

def calc_decay(created_at: str) -> tuple:
    """Calculate time decay multiplier (sigmoid: 12h grace → ~20d:0.05)."""
    if not created_at:
        return 1.0, 0, ""
    try:
        cdate = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        hours = (now - cdate).total_seconds() / 3600
        effective_hours = max(0, hours - 12)  # 12h grace
        # Sigmoid: t in hours, midpoint at 297.4h (~12.4d), 0.05 at ~20d
        decay = 1 / (1 + pow(2.71828, 0.0161 * (effective_hours - 297.4)))
        decay = max(0.05, min(1.0, decay))

        if hours < 24:
            age_str = f"{int(hours)}h"
        else:
            age_str = f"{int(hours//24)}d {int(hours%24)}h"

        return round(decay, 4), int(hours), age_str
    except:
        return 1.0, 0, "?"

def review_quality(reviews: list) -> tuple:
    """Calculate Review Quality: max(0, 1.0 - 0.15 * CR_count)."""
    cr_count = 0
    for r in reviews if isinstance(reviews, list) else []:
        if isinstance(r, dict) and r.get("state") == "CHANGES_REQUESTED":
            cr_count += 1
    quality = max(0.0, 1.0 - 0.15 * cr_count)
    return round(quality, 2), cr_count

def get_pr_age(created_at: str) -> str:
    """Get human-readable PR age."""
    _, _, age_str = calc_decay(created_at)
    return age_str

def display_dashboard(prs: list, closed: list = None):
    """Display a compact dashboard."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print()
    print(f"  PR DASHBOARD — {REPO} (alpurkan17) — {now}")
    print(f"  {'='*65}")

    # Summary
    open_count = len(prs)
    merged_count = sum(1 for p in (closed or []) if p.get("mergedAt"))
    closed_count = sum(1 for p in (closed or []) if p.get("state") == "CLOSED" and not p.get("mergedAt"))
    total_score = 0  # Would need token_score from chain

    max_prs = min(10 + total_score // 300, 30)
    print(f"  OPEN: {open_count}/{max_prs} | MERGED: {merged_count} | CLOSED (no merge): {closed_count}")

    if parsed := get_gate_status(merged_count):
        print(f"  Eligibility Gate: {'✅ PASS' if parsed['pass'] else '❌ BLOCKED'} "
              f"({parsed['merged']}/5 merged)")
    print()

    # Table header
    h = f"  {'#':<5} {'Title':<45} {'Label':<14} {'Decay':<7} {'Age':<8} {'CR':<4} {'Score':<7}"
    print(h)
    print(f"  {'-'*len(h)}")

    for pr in prs:
        num = pr["number"]
        title = pr.get("title", "")[:42]
        labels = [l["name"] for l in pr.get("labels", []) if isinstance(l, dict)]
        label_name = labels[0] if labels else "none"
        label_mult = LABEL_MULT.get(label_name.lower(), 1.0)

        decay, hours, age_str = calc_decay(pr.get("createdAt", ""))
        quality, cr_count = review_quality(pr.get("reviews", []))

        # Combined score estimate (simplified)
        combined = 1.0 * 1.33 * decay * quality * label_mult * 1.0

        decay_display = f"{decay:.3f}" if decay < 0.99 else f"{decay:.4f}"

        cr_display = f"{cr_count}" if cr_count > 0 else "0"
        if quality < 1.0:
            cr_display += "!"

        score_display = f"{combined:.3f}"
        if label_mult <= 0.3:
            score_display += "!"

        decay_flag = "⚠️" if decay < 0.5 else " "
        label_display = f"{label_name[:12]}"
        if label_mult <= 0.3:
            label_display += "!"

        print(f"  #{num:<4} {title:<45} {label_display:<14} {decay_flag}{decay_display:<7} {age_str:<8} {cr_display:<4} {score_display:<7}")

    # Action items
    print()
    items = action_items(prs, merged_count)
    if items:
        print(f"  {'='*65}")
        print(f"  ACTION ITEMS:")
        for item in items:
            print(f"    {'✅' if item['done'] else '⚠️'} {item['msg']}")
    print()

def action_items(prs: list, merged_count: int) -> list:
    """Generate action items from PR state."""
    items = []
    items.append({
        "done": merged_count >= 5,
        "msg": f"Eligibility gate: {merged_count}/5 merged PRs needed"
    })

    for pr in prs:
        num = pr["number"]
        decay, hours, _ = calc_decay(pr.get("createdAt", ""))
        labels = [l["name"] for l in pr.get("labels", []) if isinstance(l, dict)]

        if decay < 0.3:
            items.append({
                "done": False,
                "msg": f"PR #{num} decay={decay:.3f} — CLOSE and recreate with fresh branch!"
            })
        elif decay < 0.5:
            items.append({
                "done": False,
                "msg": f"PR #{num} decay={decay:.3f} — aging, consider replacement"
            })

        # Stale refactor label
        if any(l.lower() == "refactor" for l in labels):
            items.append({
                "done": False,
                "msg": f"PR #{num} has refactor label (0.25x) — title is '{pr.get('title', '')[:40]}'"
            })

        # Reviews with CHANGES_REQUESTED
        cr_count = 0
        for r in pr.get("reviews", []):
            if isinstance(r, dict) and r.get("state") == "CHANGES_REQUESTED":
                cr_count += 1
        if cr_count > 2:
            items.append({
                "done": False,
                "msg": f"PR #{num} has {cr_count} CHANGES_REQUESTED — Review Quality={max(0, 1-0.15*cr_count):.2f}x"
            })

        # Check if PR body has v3 sections
        # (Fetched separately if needed)

    return items

def get_gate_status(merged_count: int) -> dict:
    """Check eligibility gate status (≥5 merged PRs needed)."""
    return {
        "pass": merged_count >= 5,
        "merged": merged_count,
        "needed": 5,
    }

def display_json(prs: list, closed: list = None):
    """JSON output for machine parsing."""
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "repository": REPO,
        "open_count": len(prs),
        "merged_count": sum(1 for p in (closed or []) if p.get("mergedAt")),
        "closed_count": sum(1 for p in (closed or []) if not p.get("mergedAt")),
        "prs": []
    }
    for pr in prs:
        decay, hours, age_str = calc_decay(pr.get("createdAt", ""))
        quality, cr_count = review_quality(pr.get("reviews", []))
        labels = [l["name"] for l in pr.get("labels", []) if isinstance(l, dict)]
        label_mult = LABEL_MULT.get(labels[0].lower(), 1.0) if labels else 1.0

        output["prs"].append({
            "number": pr["number"],
            "title": pr["title"],
            "label": labels[0] if labels else "none",
            "label_mult": label_mult,
            "decay": decay,
            "age_hours": hours,
            "age_str": age_str,
            "review_quality": quality,
            "cr_count": cr_count,
            "estimated_score": round(1.0 * 1.33 * decay * quality * label_mult * 1.0, 4),
        })

    print(json.dumps(output, indent=2))

def main():
    p = argparse.ArgumentParser(description="PR Dashboard — one-glance status")
    p.add_argument("--watch", action="store_true", help="Watch mode (refresh every 30s)")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--action-items", action="store_true", help="Show only action items")
    args = p.parse_args()

    if args.json:
        prs = get_prs()
        closed = get_closed_prs()
        display_json(prs, closed)
        return

    if args.watch:
        try:
            while True:
                prs = get_prs()
                closed = get_closed_prs()
                display_dashboard(prs, closed)
                sleep(30)
        except KeyboardInterrupt:
            print("\n  Stopped.")
        return

    prs = get_prs()
    closed = get_closed_prs()

    if args.action_items:
        merged_count = sum(1 for p in (closed or []) if p.get("mergedAt"))
        items = action_items(prs, merged_count)
        print(f"\n  ACTION ITEMS ({len(items)}):")
        for item in items:
            print(f"    {'✅' if item['done'] else '⚠️'} {item['msg']}")
        print()
        return

    display_dashboard(prs, closed)

if __name__ == "__main__":
    main()
