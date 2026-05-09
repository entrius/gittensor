#!/usr/bin/env python3
"""
PR Body Builder — Gaya Top Merged PRs (anderdc, MkDev11, plind-junior, dll)

Cara pakai:
  python3 scripts/pr_body_builder.py --issue 123 --summary "Fix null pointer in X" \\
    --bullets "Bullet 1" "Bullet 2" --test "pytest tests/x.py"
"""
import argparse, subprocess, sys

TEMPLATE = """## Summary

{bullets}

## Related Issues

Fixes #{issue}

## Test plan

{tests}"""

def build(issue: int, summary: str, bullets: list[str], tests: list[str], detail: str = "") -> str:
    b = "\n".join(f"- {line}" for line in bullets)
    t = "\n".join(f"- [x] {line}" for line in tests)

    body = f"""## Summary

{b}
"""

    if detail:
        body += f"""\n### Why

{detail}
"""

    body += f"""
## Related Issues

Fixes #{issue}

## Test plan

{t}"""

    return body

def create_pr(title: str, body: str, head: str, dry_run: bool = False):
    if dry_run:
        print("=== DRY RUN ===")
        print(f"Title: {title}")
        print(f"Head: {head}")
        print(f"Body:\n{body}")
        return

    r = subprocess.run([
        "gh", "pr", "create",
        "--repo", "entrius/gittensor",
        "--base", "test",
        "--head", f"alpurkan17:{head}",
        "--title", title,
        "--body", body,
    ], capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        print(f"✅ PR created: {r.stdout.strip()}")
    else:
        print(f"❌ Error: {r.stderr}")
        sys.exit(1)

def parse_bullets(text: str) -> list[str]:
    return [l.strip().lstrip("- ") for l in text.strip().split("\n") if l.strip()]

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="PR Body Builder — gaya top merged PRs")
    p.add_argument("--issue", type=int, required=True, help="Issue number")
    p.add_argument("--title", required=True, help="PR title (fix:/feat: prefix)")
    p.add_argument("--bullets", nargs="+", required=True, help="Bullet points perubahan")
    p.add_argument("--tests", nargs="+", default=["ruff check", "ruff format --check"], help="Test plan items")
    p.add_argument("--detail", default="", help="Detail/Why section (opsional)")
    p.add_argument("--head", default=None, help="Branch head (default: auto dari issue)")
    p.add_argument("--dry-run", action="store_true", help="Preview only, no submit")
    args = p.parse_args()

    head = args.head or f"fix/{args.issue}-auto"
    body = build(args.issue, args.bullets[0] if args.bullets else "", args.bullets, args.tests, args.detail)

    if args.dry_run:
        create_pr(args.title, body, head, dry_run=True)
    else:
        print(f"Body PR untuk #{args.issue}:")
        print(body)
        ok = input("\nSubmit PR? (y/N): ").strip().lower()
        if ok == "y":
            create_pr(args.title, body, head)
        else:
            print("Dibatalkan.")
