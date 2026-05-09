#!/usr/bin/env python3
"""
PR Body Builder v2 — Gaya Anderdc (Top Maintainer)

Contoh:
  python3 scripts/pr_body_builder.py --issue 123 \\
    --title "fix: deskripsi (#123)" \\
    --root-cause "penyebab teknis bug" \\
    --impact "apa yang rusak" \\
    --solution "perubahan 1" "perubahan 2" \\
    --tests "ruff check" "pytest tests/cli/ - 10/10 pass" \\
    --live-verif "gitt miner check --network finney - returns correct exit code"
"""
import argparse, subprocess, sys

def build(issue: int, title: str, root_cause: str, impact: str, solution: list[str],
          tests: list[str], live_verif: list[str] = None, why: str = "") -> str:
    parts = []

    # Summary
    sol = "\n".join(f"- {s}" for s in solution)
    summary = f"## Summary\n\n{sol}"
    parts.append(summary)

    # Root Cause (untuk bug)
    if root_cause:
        parts.append(f"\n## Root Cause\n\n{root_cause}")

    # Impact
    if impact:
        parts.append(f"\n## Impact\n\n{impact}")

    # Why (konteks)
    if why:
        parts.append(f"\n### Why\n\n{why}")

    # Related Issues
    parts.append(f"\n## Related Issues\n\nFixes #{issue}")

    # Test plan
    t = "\n".join(f"- [x] {t}" for t in tests)
    parts.append(f"\n## Test plan\n\n{t}")

    # Live verification
    if live_verif:
        lv = "\n".join(f"- [x] {v}" for v in live_verif)
        parts.append(f"\n### Live verification\n\n{lv}")

    # Post-merge
    parts.append(f"\n- [ ] Post-merge: confirm fix resolves #{issue} in production")

    return "".join(parts)

def update_pr_body(pr_num: int, body: str, dry_run: bool = False):
    if dry_run:
        print("=== DRY RUN ===")
        print(f"Body untuk PR #{pr_num}:\n{body}")
        return
    r = subprocess.run(
        ["gh", "api", "--method", "PATCH", f"repos/entrius/gittensor/pulls/{pr_num}",
         "-f", f"body={body}"],
        capture_output=True, text=True, timeout=15
    )
    if r.returncode == 0:
        print(f"✅ PR #{pr_num} body updated")
    else:
        print(f"❌ Error: {r.stderr}")

def generate_upgrade(pr_num: int, issue_num: int, description: str, has_cli: bool = False):
    """Generate enhanced body untuk PR existing yang perlu di-upgrade."""
    return build(
        issue=issue_num,
        title="",
        root_cause=description,
        impact="",
        solution=[description],
        tests=["ruff check", "ruff format --check", "pyright"],
        live_verif=(["Screenshot before-after terlampir"] if has_cli else []),
    )

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="PR Body Builder v2 — gaya anderdc")
    p.add_argument("--issue", type=int, required=True, help="Issue number")
    p.add_argument("--title", required=True, help="PR title")
    p.add_argument("--root-cause", default="", help="Root cause analysis")
    p.add_argument("--impact", default="", help="Impact of the bug")
    p.add_argument("--solution", nargs="+", required=True, help="Changes made")
    p.add_argument("--why", default="", help="Background context")
    p.add_argument("--tests", nargs="+", default=["ruff check"], help="Test plan items")
    p.add_argument("--live-verif", nargs="*", default=[], help="Live verification commands")
    p.add_argument("--dry-run", action="store_true", help="Preview only")
    p.add_argument("--update-pr", type=int, default=None, help="Update existing PR body")
    args = p.parse_args()

    body = build(
        issue=args.issue, title=args.title,
        root_cause=args.root_cause, impact=args.impact,
        solution=args.solution, tests=args.tests,
        live_verif=args.live_verif, why=args.why,
    )

    if args.update_pr:
        update_pr_body(args.update_pr, body, args.dry_run)
    else:
        print(f"\nBody PR untuk #{args.issue}:\n")
        print(body)
        print(f"\n{'─' * 60}")
        if not args.dry_run:
            ok = input("\nSubmit PR? (y/N): ").strip().lower()
            if ok == "y":
                r = subprocess.run([
                    "gh", "pr", "create", "--repo", "entrius/gittensor",
                    "--base", "test", "--head", f"alpurkan17:{args.title.split()[0].replace(':','')}-{args.issue}",
                    "--title", args.title, "--body", body,
                ], capture_output=True, text=True, timeout=30)
                if r.returncode == 0:
                    print(f"✅ {r.stdout.strip()}")
                else:
                    print(f"❌ {r.stderr}")
            else:
                print("Dibatalkan.")
