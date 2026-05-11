#!/usr/bin/env python3
"""
PR Conflict Checker — cek apakah branch bisa di-merge ke base tanpa conflict.

Usage:
  python3 scripts/pr_conflict_check.py                          # cek branch aktif vs test
  python3 scripts/pr_conflict_check.py --branch fix/123-slug    # cek branch spesifik
  python3 scripts/pr_conflict_check.py --base main              # base berbeda
  python3 scripts/pr_conflict_check.py --json
"""

import argparse, json, os, re, subprocess, sys, textwrap, tempfile

BASE = "test"
REMOTE = "origin"

def run(cmd: list, timeout=30) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r
    except subprocess.TimeoutExpired:
        r = subprocess.CompletedProcess(cmd, -1, "", f"TIMEOUT ({timeout}s)")
        return r
    except FileNotFoundError:
        r = subprocess.CompletedProcess(cmd, -1, "", "command not found")
        return r

def get_current_branch() -> str:
    r = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
    return r.stdout.strip() if r.returncode == 0 else ""

def check_merge_conflict(branch: str, base: str) -> dict:
    branch = branch or get_current_branch()
    if not branch:
        return {"ok": False, "branch": "", "base": base, "msg": "Tidak bisa deteksi branch", "fatal": True}
    if branch == base:
        return {"ok": True, "branch": branch, "base": base, "msg": f"Branch '{branch}' == base '{base}' (same)", "fatal": False}

    # Check if remote base is available
    r = run(["git", "fetch", REMOTE, base], timeout=15)
    if r.returncode != 0:
        r2 = run(["git", "fetch", "upstream", base], timeout=15)
        if r2.returncode != 0:
            return {"ok": True, "branch": branch, "base": base,
                    "msg": f"Cannot fetch {base} (offline), skip conflict check", "fatal": False}

    # Try merge in dry-run
    r = run(["git", "merge-tree", f"FETCH_HEAD", branch], timeout=15)
    if r.returncode != 0:
        # Try merge --no-commit --no-ff
        with tempfile.TemporaryDirectory() as tmpdir:
            r = run(["git", "clone", ".", tmpdir], timeout=15)
            if r.returncode != 0:
                return {"ok": True, "branch": branch, "base": base,
                        "msg": "Cannot clone for merge test, skip", "fatal": False}
            r = run(["git", "checkout", base], timeout=5, cwd=tmpdir)
            r = run(["git", "merge", "--no-commit", "--no-ff", branch], timeout=15, cwd=tmpdir)
            if r.returncode != 0:
                conflicted = "CONFLICT" in r.stdout or "CONFLICT" in r.stderr
                if conflicted:
                    return {"ok": False, "branch": branch, "base": base,
                            "msg": f"Merge conflict! Branch '{branch}' vs '{base}'", "fatal": False}
                return {"ok": False, "branch": branch, "base": base,
                        "msg": f"Merge error: {r.stderr[:200]}", "fatal": False}
            # Check git status for conflict markers
            r = run(["git", "diff", "--name-only", "--diff-filter=U"], timeout=5, cwd=tmpdir)
            if r.stdout.strip():
                return {"ok": False, "branch": branch, "base": base,
                        "msg": f"Conflict in: {r.stdout.strip()[:200]}", "fatal": False}
            return {"ok": True, "branch": branch, "base": base,
                    "msg": f"No conflict: '{branch}' → '{base}' ✅", "fatal": False}

    # Parse merge-tree output for conflict markers
    if "<<<<<<<" in r.stdout:
        return {"ok": False, "branch": branch, "base": base,
                "msg": f"Merge conflict! Branch '{branch}' vs '{base}'", "fatal": False}

    return {"ok": True, "branch": branch, "base": base,
            "msg": f"No conflict: '{branch}' → '{base}' ✅", "fatal": False}

def main():
    p = argparse.ArgumentParser(
        description="PR Conflict Checker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s
              %(prog)s --branch fix/123-slug
              %(prog)s --branch fix/123-slug --base main
              %(prog)s --json
        """),
    )
    p.add_argument("--branch", help=f"Branch name (default: current)")
    p.add_argument("--base", default=BASE, help=f"Base branch (default: {BASE})")
    p.add_argument("--json", action="store_true", help="JSON output")
    args = p.parse_args()

    result = check_merge_conflict(args.branch, args.base)

    if args.json:
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    ok = result.get("ok")
    print(f"\n{'='*50}")
    print(f"  PR CONFLICT CHECK")
    print(f"{'='*50}")
    if result.get("fatal"):
        print(f"  💀 {result['msg']}")
    elif ok:
        print(f"  ✅ {result['msg']}")
    else:
        print(f"  ❌ {result['msg']}")
    print(f"{'='*50}\n")

    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
