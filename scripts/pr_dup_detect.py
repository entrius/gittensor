#!/usr/bin/env python3
"""
PR Duplicate Detector — cek apakah issue sudah punya PR open.

Cek dari:
  - PR kita sendiri (alpurkan17)
  - PR orang lain (kompetitor)

Usage:
  python3 scripts/pr_dup_detect.py --issue 123
  python3 scripts/pr_dup_detect.py --issue 123 --json
  python3 scripts/pr_dup_detect.py --issue 123 --watch   # exit 1 kalau duplikat
"""

import argparse, json, os, re, subprocess, sys, textwrap

REPO = "entrius/gittensor"
FORK = "alpurkan17/gittensor"
FORK_OWNER = FORK.split("/")[0]

def run(cmd: list, timeout=20) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, -1, "", f"TIMEOUT ({timeout}s)")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, -1, "", "command not found")

def check_duplicate(issue: int, repo: str = REPO, fork_owner: str = FORK_OWNER) -> dict:
    r = run([
        "gh", "pr", "list", "--repo", repo, "--state", "open",
        "--json", "number,title,author,headRefName",
        "--search", f"#{issue} in:title", "-L", "20"
    ], timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        return {"ok": True, "issue": issue, "our_prs": [], "competitors": [],
                "msg": f"Tidak ada PR untuk issue #{issue}", "fatal": False}

    try:
        prs = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": True, "issue": issue, "our_prs": [], "competitors": [],
                "msg": f"Gagal parse PR list", "fatal": False}

    our_prs = [p for p in prs if p.get("author", {}).get("login") == fork_owner]
    competitors = [p for p in prs if p.get("author", {}).get("login") != fork_owner]

    ok = len(our_prs) == 0
    msg_parts = []

    if our_prs:
        nums = [str(p["number"]) for p in our_prs]
        msg_parts.append(f"PR kita sendiri: #{', #'.join(nums)}")
    if competitors:
        nums = [f"#{p['number']} (@{p['author']['login']})" for p in competitors]
        msg_parts.append(f"Kompetitor: {', '.join(nums)}")
    if not our_prs and not competitors:
        msg_parts.append(f"Tidak ada PR untuk issue #{issue}")

    return {
        "ok": ok,
        "issue": issue,
        "our_prs": [p["number"] for p in our_prs],
        "competitors": [{"number": p["number"], "author": p["author"]["login"]} for p in competitors],
        "msg": " — ".join(msg_parts) if msg_parts else f"OK",
        "fatal": False,
    }

def main():
    p = argparse.ArgumentParser(
        description="PR Duplicate Detector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s --issue 123
              %(prog)s --issue 123 --json
              %(prog)s --issue 123 --watch
        """),
    )
    p.add_argument("--issue", type=int, required=True, help="Issue number")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--watch", action="store_true", help="Exit 1 jika ada duplikat")
    args = p.parse_args()

    result = check_duplicate(args.issue)

    if args.json:
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    ok = result.get("ok")
    our_prs = result.get("our_prs", [])
    competitors = result.get("competitors", [])

    print(f"\n{'='*50}")
    print(f"  PR DUPLICATE DETECTOR — Issue #{args.issue}")
    print(f"{'='*50}")
    if our_prs:
        print(f"  ❌ PR kita sendiri: #{', #'.join(str(p) for p in our_prs)}")
    else:
        print(f"  ✅ Tidak ada PR kita untuk issue ini")
    if competitors:
        for c in competitors:
            print(f"  ⚠️  Kompetitor: #{c['number']} (@{c['author']})")
    else:
        print(f"  ✅ Tidak ada kompetitor")
    print(f"{'='*50}")
    if our_prs:
        print(f"  ❌ DUPLIKAT — sudah punya PR untuk issue ini")
    elif competitors:
        print(f"  ⚠️  Ada kompetitor — masih bisa buat PR")
    else:
        print(f"  ✅ AMAN — belum ada PR untuk issue #{args.issue}")
    print(f"{'='*50}\n")

    if args.watch and our_prs:
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
