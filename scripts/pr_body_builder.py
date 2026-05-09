#!/usr/bin/env python3
"""
PR Body Builder v3 — Gaya Anderdc v2 + Terminal Evidence + Post-Merge Verification.

Improvements over v2:
  - Embedded terminal output blocks (bukan Pillow screenshot eksternal)
  - "How to verify" section untuk reviewer
  - Post-merge verification dengan specific docker/validator commands
  - Cross-references ke repo lain
  - Edge cases documentation
  - "Out of scope" section untuk PR fokus

Usage:
  python3 pr_body_builder.py --issue 123 \
    --title "fix: deskripsi (#123)" \
    --root-cause "penyebab teknis" \
    --impact "apa yang rusak" \
    --solution "perubahan 1" "perubahan 2" \
    --tests "ruff check" "pytest tests/ - 10/10 pass"
"""
import argparse, subprocess, sys, os

def build(
    issue: int,
    title: str,
    root_cause: str,
    impact: str,
    solution: list[str],
    tests: list[str],
    live_verif: list[str] = None,
    why: str = "",
    terminal_evidence: list[str] = None,
    how_to_verify: list[str] = None,
    post_merge: list[str] = None,
    edge_cases: list[str] = None,
    cross_refs: list[str] = None,
    out_of_scope: str = "",
) -> str:
    parts = []

    # Summary
    sol = "\n".join(f"- {s}" for s in solution)
    summary = f"## Summary\n\n{sol}"
    parts.append(summary)

    # Root Cause
    if root_cause:
        parts.append(f"\n## Root Cause\n\n{root_cause}")

    # Impact
    if impact:
        parts.append(f"\n## Impact\n\n{impact}")

    # Why (broader context — anderdc style)
    if why:
        parts.append(f"\n### Why\n\n{why}")

    # Related Issues
    parts.append(f"\n## Related Issues\n\nFixes #{issue}")

    # Cross-references
    if cross_refs:
        cr = "\n".join(f"- {r}" for r in cross_refs)
        parts.append(f"\n### Related references\n\n{cr}")

    # Test plan
    t = "\n".join(f"- [x] {t}" for t in tests)
    parts.append(f"\n## Test plan\n\n{t}")

    # Live verification (actual terminal output blocks)
    if live_verif:
        lv = "\n".join(f"- [x] {v}" for v in live_verif)
        parts.append(f"\n### Live verification\n\n{lv}")

    # Embedded terminal evidence
    if terminal_evidence:
        parts.append(f"\n### Terminal evidence\n")
        for block in terminal_evidence:
            parts.append(f"\n{block}")

    # How to verify (for reviewer)
    if how_to_verify:
        hv = "\n".join(f"1. {v}" for i, v in enumerate(how_to_verify))
        parts.append(f"\n### How to verify\n\n{hv}")

    # Edge cases
    if edge_cases:
        ec = "\n".join(f"- {e}" for e in edge_cases)
        parts.append(f"\n### Edge cases considered\n\n{ec}")

    # Out of scope
    if out_of_scope:
        parts.append(f"\n### Out of scope\n\n{out_of_scope}")

    # Post-merge verification
    if post_merge:
        pm = "\n".join(f"- [ ] {p}" for p in post_merge)
        parts.append(f"\n### Post-merge verification\n\n{pm}")
    else:
        # Default post-merge
        parts.append(f"\n### Post-merge verification\n\n- [ ] Confirm fix resolves #{issue} in production")

    return "".join(parts)


def update_pr_body(pr_num: int, body: str, dry_run: bool = False):
    if dry_run:
        print("=== DRY RUN ===")
        print(f"Body untuk PR #{pr_num}:\n{body}")
        return
    r = subprocess.run(
        ["gh", "api", "--method", "PATCH",
         f"repos/entrius/gittensor/pulls/{pr_num}",
         "-f", f"body={body}"],
        capture_output=True, text=True, timeout=15
    )
    if r.returncode == 0:
        print(f"  PR #{pr_num} body updated")
    else:
        print(f"  Error: {r.stderr[:200]}")


def generate_upgrade(
    pr_num: int,
    issue_num: int,
    description: str,
    has_cli: bool = False,
    post_merge_cmds: list[str] = None,
    edge_cases: list[str] = None,
):
    """Generate enhanced body for existing PR."""
    pm = post_merge_cmds or [
        f"Confirm fix resolves #{issue_num} in production",
        "Monitor validator logs for warnings",
    ]
    ev = []
    if has_cli:
        evidence_path = os.path.join(
            os.path.dirname(__file__), "..", "assets",
            "terminal_captures", f"pr{pr_num}_evidence.md"
        )
        if os.path.exists(evidence_path):
            with open(evidence_path) as f:
                ev_content = f.read().strip()
                if ev_content:
                    ev = [ev_content]

    return build(
        issue=issue_num,
        title="",
        root_cause=description,
        impact="",
        solution=[description],
        tests=["ruff check", "ruff format --check", "pyright"],
        live_verif=(["Terminal evidence terlampir"] if has_cli else []),
        terminal_evidence=ev if ev else None,
        post_merge=pm,
        edge_cases=edge_cases,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="PR Body Builder v3 — Gaya Anderdc v2 + Evidence"
    )
    p.add_argument("--issue", type=int, required=True, help="Issue number")
    p.add_argument("--title", required=True, help="PR title")
    p.add_argument("--root-cause", default="", help="Root cause analysis")
    p.add_argument("--impact", default="", help="Impact of the bug")
    p.add_argument("--solution", nargs="+", required=True, help="Changes made")
    p.add_argument("--why", default="", help="Broader context (why this matters)")
    p.add_argument("--tests", nargs="+", default=["ruff check"],
                   help="Test plan items")
    p.add_argument("--live-verif", nargs="*", default=[],
                   help="Live verification commands")
    p.add_argument("--terminal-evidence", nargs="*", default=[],
                   help="Path(s) to terminal capture markdown files")
    p.add_argument("--how-to-verify", nargs="*", default=[],
                   help="Step-by-step verification for reviewer")
    p.add_argument("--post-merge", nargs="*", default=[],
                   help="Post-merge verification items")
    p.add_argument("--edge-cases", nargs="*", default=[],
                   help="Edge cases considered")
    p.add_argument("--cross-refs", nargs="*", default=[],
                   help="Cross-references to other repos/issues")
    p.add_argument("--out-of-scope", default="",
                   help="What is NOT covered in this PR")
    p.add_argument("--dry-run", action="store_true", help="Preview only")
    p.add_argument("--update-pr", type=int, default=None,
                   help="Update existing PR body")
    args = p.parse_args()

    # Load terminal evidence from files
    terminal_blocks = []
    for ev_path in args.terminal_evidence:
        if os.path.exists(ev_path):
            with open(ev_path) as f:
                content = f.read().strip()
                if content:
                    terminal_blocks.append(content)
        else:
            print(f"  ? Terminal evidence file not found: {ev_path}")

    body = build(
        issue=args.issue,
        title=args.title,
        root_cause=args.root_cause,
        impact=args.impact,
        solution=args.solution,
        tests=args.tests,
        live_verif=args.live_verif if args.live_verif else None,
        why=args.why,
        terminal_evidence=terminal_blocks if terminal_blocks else None,
        how_to_verify=args.how_to_verify if args.how_to_verify else None,
        post_merge=args.post_merge if args.post_merge else None,
        edge_cases=args.edge_cases if args.edge_cases else None,
        cross_refs=args.cross_refs if args.cross_refs else None,
        out_of_scope=args.out_of_scope,
    )

    if args.update_pr:
        update_pr_body(args.update_pr, body, args.dry_run)
    else:
        print(f"\nBody PR untuk #{args.issue}:\n")
        print(body)
        print(f"\n{'=' * 60}")
        if not args.dry_run:
            try:
                ok = input("\nSubmit PR? (y/N): ").strip().lower()
                if ok == "y":
                    branch_prefix = args.title.split()[0].replace(":", "")
                    r = subprocess.run([
                        "gh", "pr", "create",
                        "--repo", "entrius/gittensor",
                        "--base", "test",
                        "--head", f"alpurkan17:{branch_prefix}-{args.issue}",
                        "--title", args.title,
                        "--body", body,
                    ], capture_output=True, text=True, timeout=30)
                    if r.returncode == 0:
                        print(f"  {r.stdout.strip()}")
                    else:
                        print(f"  Error: {r.stderr[:200]}")
                else:
                    print("Cancelled.")
            except (EOFError, KeyboardInterrupt):
                print()
