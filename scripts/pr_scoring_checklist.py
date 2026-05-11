#!/usr/bin/env python3
"""PR Scoring Checklist — validasi 5 faktor scoring sebelum buat PR."""

import json
import os
import re
import subprocess
import sys

REPO = "entrius/gittensor"
FORK = "alpurkan17/gittensor"
BASE = "test"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_HEADERS = ["-H", f"Authorization: bearer {GITHUB_TOKEN}"] if GITHUB_TOKEN else []


def run(cmd: list, timeout=30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        r = subprocess.CompletedProcess(cmd, -1, "", "command not found")
        return r


def get_repo_weight(repo_name: str = REPO) -> float:
    r = run([
        "gh", "api",
        f"repos/entrius/gittensor/contents/gittensor/validator/weights/master_repositories.json",
        "--jq", ".content"
    ] + GH_HEADERS, timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        return 0.0
    try:
        import base64
        content = base64.b64decode(r.stdout.strip()).decode()
        data = json.loads(content)
        for entry in data if isinstance(data, list) else data.get("repositories", data if isinstance(data, dict) else []):
            if isinstance(entry, dict) and entry.get("name", entry.get("repo")) == repo_name:
                return float(entry.get("weight", entry.get("repo_weight", 0)))
            if isinstance(data, dict) and repo_name in data:
                return float(data[repo_name].get("weight", 0))
    except Exception:
        pass
    return 0.0


def check_repo_weight(repo_name: str = REPO) -> dict:
    w = get_repo_weight(repo_name)
    ok = w >= 0.5
    return {"ok": ok, "weight": w, "msg": f"Repo weight {repo_name}: {w:.4f}" + (" ✅" if ok else " ⚠️ < 0.5")}


def check_issue_bonus(issue: int) -> dict:
    r = run([
        "gh", "issue", "view", str(issue), "--repo", REPO,
        "--json", "author,title,labels,state"
    ] + GH_HEADERS, timeout=15)
    if r.returncode != 0:
        return {"ok": False, "bonus": 1.0, "is_maintainer": False, "msg": "Could not fetch issue"}
    try:
        data = json.loads(r.stdout)
        author = data.get("author", {})
        is_maintainer = author.get("login") in ["anderdc", "landyndev"] if isinstance(author, dict) else False
        bonus = 1.66 if is_maintainer else 1.33
        return {
            "ok": True,
            "bonus": bonus,
            "is_maintainer": is_maintainer,
            "msg": f"Issue #{issue}: {'maintainer (1.66x)' if is_maintainer else 'standard (1.33x)'} bonus ✅"
        }
    except Exception:
        return {"ok": False, "bonus": 1.0, "is_maintainer": False, "msg": "Could not parse issue"}


def check_collateral(open_count: int = None) -> dict:
    if open_count is None:
        r = run([
            "gh", "pr", "list", "--repo", REPO, "--author", "alpurkan17",
            "--state", "open", "--json", "number", "-L", "50"
        ] + GH_HEADERS, timeout=15)
        open_count = len(json.loads(r.stdout)) if r.returncode == 0 and r.stdout.strip() else 0
    locked = open_count * 1.72
    ok = open_count < 10
    return {
        "ok": ok,
        "open": open_count,
        "locked": locked,
        "msg": f"Collateral: {open_count} PR × 1.72τ = {locked:.2f}τ locked{' ✅' if ok else ' ⚠️ >= 10 PR, threshold near'}"
    }


def check_label(prefix: str) -> dict:
    label_map = {"fix": ("bug", 1.1), "feat": ("enhancement", 1.25)}
    if prefix not in label_map:
        return {"ok": False, "label": "none", "mult": 0.0, "msg": f"Prefix '{prefix}' not allowed. Use fix: (1.1x) or feat: (1.25x)"}
    label, mult = label_map[prefix]
    ok = mult >= 1.1
    return {"ok": ok, "label": label, "mult": mult, "msg": f"Label: {label} ({mult}x) ✅"}


def check_code_density(diff_lines: int = None, files: int = None) -> dict:
    if diff_lines is None or files is None:
        r = run(["git", "diff", "--stat", f"{BASE}..."], timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            lines = r.stdout.strip().split("\n")
            for l in lines:
                m = re.search(r'(\d+) file', l)
                if m:
                    files = int(m.group(1))
                m = re.search(r'(\d+) insertions', l)
                if m:
                    diff_lines = int(m.group(1))
    if diff_lines is None:
        diff_lines = 0
    if files is None:
        files = 0
    if diff_lines == 0 and files == 0:
        return {"ok": True, "lines": 0, "files": 0, "msg": "Code density: no changes (clean diff) ✅"}
    msg_parts = []
    if diff_lines >= 100:
        msg_parts.append(f"lines {diff_lines} >= 100")
    if files > 4:
        msg_parts.append(f"files {files} > 4")
    if not files:
        msg_parts.append("no files (diff empty)")
    if ok:
        return {"ok": True, "lines": diff_lines, "files": files, "msg": f"Code density: {diff_lines} lines, {files} files ✅"}
    return {"ok": False, "lines": diff_lines, "files": files, "msg": f"Code density: {diff_lines} lines, {files} files — {' + '.join(msg_parts)} ⚠️"}


def run_checklist(issue: int = None, prefix: str = "fix", repo: str = REPO, diff_lines: int = None, files: int = None, open_count: int = None) -> list:
    checks = []
    checks.append(check_repo_weight(repo))
    if issue:
        checks.append(check_issue_bonus(issue))
    checks.append(check_collateral(open_count))
    checks.append(check_label(prefix))
    checks.append(check_code_density(diff_lines, files))
    return checks


def print_checklist(checks: list, prefix: str = "  "):
    all_ok = True
    print(f"\n{prefix}{'=' * 50}")
    print(f"{prefix}  PR SCORING CHECKLIST")
    print(f"{prefix}{'=' * 50}")
    for c in checks:
        mark = "✅" if c["ok"] else "❌"
        print(f"{prefix}  {mark}  {c['msg']}")
        if not c["ok"]:
            all_ok = False
    print(f"{prefix}{'=' * 50}")
    print(f"{prefix}  {'✅ Semua OK — siap buat PR' if all_ok else '❌ Ada masalah — perbaiki dulu'}")
    print(f"{prefix}{'=' * 50}")
    return all_ok


def main():
    import argparse
    p = argparse.ArgumentParser(description="PR Scoring Checklist")
    p.add_argument("--issue", type=int, help="Issue number for bonus check")
    p.add_argument("--prefix", default="fix", help="Prefix: fix or feat")
    p.add_argument("--repo", default=REPO, help=f"Repo (default: {REPO})")
    p.add_argument("--diff-lines", type=int, help="Diff line count (auto if not set)")
    p.add_argument("--files", type=int, help="File count (auto if not set)")
    p.add_argument("--open-count", type=int, help="Open PR count (auto if not set)")
    p.add_argument("--json", action="store_true", help="JSON output")
    args = p.parse_args()

    checks = run_checklist(
        issue=args.issue,
        prefix=args.prefix,
        repo=args.repo,
        diff_lines=args.diff_lines,
        files=args.files,
        open_count=args.open_count,
    )

    if args.json:
        print(json.dumps(checks, indent=2))
        return

    all_ok = print_checklist(checks)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
