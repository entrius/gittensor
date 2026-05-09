import json, subprocess, sys
from datetime import datetime, timezone
from math import exp

REPO = "entrius/gittensor"
REPO_WEIGHT = 1.0
TOP_REPOS = ["entrius/gittensor", "entrius/allways", "entrius/gittensor-ui", "entrius/allways-ui"]

LABEL_MULTIPLIERS = {
    "feature": 1.5,
    "enhancement": 1.25,
    "bug": 1.1,
    "refactor": 0.25,
}
DEFAULT_LABEL = 1.0

ISSUE_MULTIPLIER_NONE = 1.0
ISSUE_MULTIPLIER_STANDARD = 1.33
ISSUE_MULTIPLIER_MAINTAINER = 1.66

DECAY_K = 0.0161
DECAY_T0 = 297.4
GRACE_HOURS = 12

def time_decay(hours):
    if hours <= GRACE_HOURS:
        return 1.0
    return 1.0 / (1.0 + exp(DECAY_K * (hours - DECAY_T0)))

def check_prs():
    r = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO, "--state", "open",
         "--author", "@me", "--json", "number,title,labels,createdAt,body,headRefName,baseRefName",
         "--limit", "20"], capture_output=True, text=True, timeout=30
    )
    if r.returncode != 0:
        print(f"Error: {r.stderr}"); return
    prs = json.loads(r.stdout)
    now = datetime.now(timezone.utc)

    print("=" * 78)
    print("  PR QUALITY CHECK — 7 FAKTOR WAJIB")
    print("=" * 78)

    for pr in sorted(prs, key=lambda x: x["createdAt"]):
        n = pr["number"]
        title = pr["title"]
        labels = [l["name"] for l in pr.get("labels", [])]
        body = pr.get("body", "")
        head = pr.get("headRefName", "")
        base = pr.get("baseRefName", "")
        created = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
        hours = (now - created).total_seconds() / 3600

        issues = 0
        label_mult = DEFAULT_LABEL
        label_name = "none"

        for l in labels:
            lm = LABEL_MULTIPLIERS.get(l.lower())
            if lm is not None:
                label_mult = lm
                label_name = l

        has_issue_ref = any(kw in body for kw in ["Closes #", "Fixes #", "Resolves #", "Close #", "Fix #"])
        decay = time_decay(hours)
        branch_ok = base == "test"
        template_ok = "## Summary" in body and "## Related Issues" in body
        review_mult = 1.0

        print(f"\n  #{n}: {title}")
        print(f"  {'─' * 74}")

        checks = []
        total = 0.0

        # 1. Repo Weight
        if head.startswith("fix/") or head.startswith("feat/"):
            checks.append(("Repo Weight", REPO_WEIGHT, f"✓ repo=entrius/gittensor({REPO_WEIGHT}x)", REPO_WEIGHT > 0.1))
        else:
            checks.append(("Repo Weight", REPO_WEIGHT, f"entrius/gittensor ({REPO_WEIGHT}x)", True))
        total += REPO_WEIGHT

        # 2. Issue Bonus
        if has_issue_ref:
            checks.append(("Issue Bonus", ISSUE_MULTIPLIER_STANDARD, "✓ Ada Closes/Fixes #N (1.33x)", True))
            total *= 1.33
        else:
            checks.append(("Issue Bonus", 1.0, "✗ TIDAK ADA Closes/Fixes #N (1.0x)", False))
            total *= 1.0

        # 3. Credibility (estimated)
        checks.append(("Credibility", 1.0, "~ (belum ada merged/closed)", True))

        # 4. Review Quality
        checks.append(("Review Quality", 1.0, "✓ 0 CR (belum direview)", True))

        # 5. Time Decay
        d, hrs = int(hours // 24), int(hours % 24)
        age = f"{d}d {hrs}h" if d else f"{int(hours)}h"
        decay_ok = decay >= 0.50
        checks.append(("Time Decay", round(decay, 4), f"{'✓' if decay_ok else '✗'} {age} → decay={decay:.4f}", decay_ok))

        # 6. Label
        label_ok = label_mult >= 1.1
        checks.append(("Label", label_mult, f"{'✓' if label_ok else '✗'} label={label_name} ({label_mult}x)", label_ok))
        total *= label_mult

        # 7. Code Density (estimated)
        checks.append(("Code Density", 1.0, "~ (dihitung validators)", True))

        print(f"  {'Faktor':<18} {'Mult':<8} {'Status'}")
        print(f"  {'─' * 18} {'─' * 8} {'─' * 44}")
        for name, mult, note, ok in checks:
            mark = "✅" if ok else "❌"
            print(f"  {name:<18} {mult:<8} {mark} {note}")

        print(f"  {'─' * 74}")
        if has_issue_ref and label_ok and decay_ok and branch_ok:
            print(f"  ✅ PR #{n} LAYAK — semua faktor utama terpenuhi")
        else:
            print(f"  ⚠️  PR #{n} perlu perbaikan:" if not label_ok or not has_issue_ref else f"  ✅ PR #{n} OK")

    print("\n" + "=" * 78)
    closed_r = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO, "--state", "closed", "--author", "@me",
         "--json", "number,state,mergedAt", "--limit", "20"],
        capture_output=True, text=True, timeout=15
    )
    if closed_r.returncode == 0:
        closed = json.loads(closed_r.stdout)
        merged = sum(1 for p in closed if p.get("mergedAt"))
        total_closed = len(closed)
        cred = merged / (merged + total_closed - 1) if (merged + total_closed - 1) > 0 else 0
        print(f"\n  Credibility: {merged} merged / {merged + total_closed - 1} total = {cred:.2%}")
        print(f"  Target: ≥80% → {'✅ LULUS' if cred >= 0.8 else '❌ BELUM'}")
        print(f"  Eligibility Gate: ≥5 merged PRs → {'✅' if merged >= 5 else '❌ 0/5 merged'}")

if __name__ == "__main__":
    check_prs()
