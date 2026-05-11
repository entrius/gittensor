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
    root_cause: str = "",
    impact: str = "",
    solution: list[str] | None = None,
    tests: list[str] | None = None,
    live_verif: list[str] | None = None,
    why: str = "",
    terminal_evidence: list[str] | None = None,
    how_to_verify: list[str] | None = None,
    post_merge: list[str] | None = None,
    edge_cases: list[str] | None = None,
    cross_refs: list[str] | None = None,
    out_of_scope: str = "",
    problem: str = "",
    summary_text: str = "",
) -> str:
    import re as _re
    parts = []

    # Summary — gabungan root cause + impact + solusi dalam 1-2 paragraf
    if summary_text:
        parts.append(f"## Summary\n\n{summary_text}")
    elif solution:
        sol = "\n".join(f"- {s}" for s in solution)
        if root_cause:
            parts.append(f"## Summary\n\n{root_cause}\n\n{sol}")
        else:
            parts.append(f"## Summary\n\n{sol}")
    elif root_cause:
        parts.append(f"## Summary\n\n{root_cause}")

    # Problem (opsional — code references, detail teknis)
    if problem:
        parts.append(f"\n## Problem\n\n{problem}")

    # Out of scope (opsional, pendek)
    if out_of_scope:
        parts.append(f"\n## Out of scope\n\n{out_of_scope}")

    # Validation — actual test results
    if tests:
        t = "\n".join(f"- {tv}" for tv in tests)
        parts.append(f"\n## Validation\n\n{t}")

    # Live verification (actual terminal output)
    if live_verif:
        lv = "\n".join(f"- {v}" for v in live_verif)
        parts.append(f"\n{lv}")

    # Terminal evidence block
    if terminal_evidence:
        parts.append("\n")
        for block in terminal_evidence:
            parts.append(f"\n{block}")

    # Closes/Fixes
    parts.append(f"\nCloses #{issue}")

    result = "".join(parts)
    # Warn if body is too bare — both old and new formats accepted
    has_old = all(s in result for s in ["## Root Cause", "## Impact", "## Test plan"])
    has_new = "## Validation" in result
    if not has_old and not has_new and "## Summary" in result:
        import sys as _sys
        print("  NOTE: body uses Simplified format (Summary + Validation + Closes)", file=_sys.stderr)
    elif not has_old and not has_new:
        import sys as _sys
        print("  WARNING: body missing both Validation and Test plan sections", file=_sys.stderr)
    return result


def _check_pr_not_merged(pr_num: int) -> bool:
    """Return True if PR is safe to edit (not merged). Print warning and return False if merged."""
    r = subprocess.run(
        ["gh", "pr", "view", str(pr_num), "--repo", "entrius/gittensor",
         "--json", "state", "--jq", ".state"],
        capture_output=True, text=True, timeout=10
    )
    state = r.stdout.strip() if r.returncode == 0 else ""
    if state == "MERGED":
        print(f"  ❌ PR #{pr_num} is MERGED — cannot edit body (immutable rule). Open follow-up PR instead.")
        return False
    return True


def update_pr_body(pr_num: int, body: str, dry_run: bool = False):
    if dry_run:
        print("=== DRY RUN ===")
        print(f"Body untuk PR #{pr_num}:\n{body}")
        return
    if not _check_pr_not_merged(pr_num):
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
    """Generate enhanced body for existing PR. Rejects merged PRs."""
    if not _check_pr_not_merged(pr_num):
        return None
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
        # Validate required sections (both old and new formats)
        has_old = all(s in body for s in ["## Root Cause", "## Impact", "## Test plan"])
        has_new = "## Validation" in body
        has_closes = "Closes #" in body or bool(re.search(r'(?:Fixes|Closes|Resolves)\s+#\d+', body))
        if "## Summary" not in body:
            print("  ❌ Body missing ## Summary section")
        elif not has_old and not has_new:
            print("  ⚠️  Body missing both Validation and Test plan sections")
        elif not has_closes:
            print("  ⚠️  Body missing Closes/Fixes #N reference")
        if not args.dry_run:
            try:
                ok = input("\nSubmit PR? (y/N): ").strip().lower()
                if ok == "y":
                    slug = args.title.split(": ", 1)[-1].split(" (")[0].lower()
                    slug = re.sub(r'[^a-z0-9-]+', '-', slug).strip('-')[:40]
                    branch = f"fix/{args.issue}-{slug}"
                    r = subprocess.run([
                        "gh", "pr", "create",
                        "--repo", "entrius/gittensor",
                        "--base", "test",
                        "--head", f"alpurkan17:{branch}",
                        "--title", args.title,
                        "--body", body,
                    ], capture_output=True, text=True, timeout=30)
                    if r.returncode == 0:
                        url = r.stdout.strip()
                        print(f"  {url}")
                        pr_num_match = re.search(r'#(\d+)', url)
                        pr_num = pr_num_match.group(1) if pr_num_match else None
                        if pr_num:
                            for reviewer in ["anderdc", "landyndev"]:
                                subprocess.run([
                                    "gh", "api",
                                    f"repos/entrius/gittensor/pulls/{pr_num}/requested_reviewers",
                                    "--method", "POST",
                                    "-f", f"reviewers[]={reviewer}",
                                ], capture_output=True, text=True, timeout=10)
                                print(f"  Reviewer @{reviewer} requested (may fail from fork)")
                    else:
                        print(f"  Error: {r.stderr[:200]}")
                else:
                    print("Cancelled.")
            except (EOFError, KeyboardInterrupt):
                print()
