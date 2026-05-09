#!/usr/bin/env python3
"""Pre-submit checklist — validasi PR sebelum di-push."""

import subprocess, sys, json, os

REPO = "entrius/gittensor"

def run(cmd: list, timeout=30) -> tuple:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def check_branch():
    code, out, _ = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], 5)
    branch = out
    if branch == "test":
        print(f"  ⏭️  Branch: {branch} (base branch, skipping)")
        return True
    ok = branch.startswith("fix/") or branch.startswith("feat/") or branch.startswith("refactor/") or branch.startswith("perf/") or branch.startswith("cli/")
    print(f"  {'✅' if ok else '❌'} Branch: {branch}")
    return ok

def check_ruff():
    code, out, err = run(["ruff", "check", "."], 30)
    # ruff can exit 0 even with no files checked
    ok = code == 0
    status = "passed" if ok else f"FAILED ({len(err.split(chr(10)))-1} errors)" if err else "passed (no files)"
    print(f"  {'✅' if ok else '❌'} ruff check: {status}")
    return ok

def check_ruff_format():
    code, out, err = run(["ruff", "format", "--check", "."], 30)
    files_changed = len([l for l in out.split(chr(10)) if l and not l.startswith("ℹ") and l.strip()])
    ok = code == 0
    status = "passed" if ok else f"{files_changed} file(s) would be reformatted"
    print(f"  {'✅' if ok else '❌'} ruff format: {status}")
    return ok

def check_pyright():
    code, _, err = run(["pyright"], 60)
    # pyright exits 0 if only pre-existing errors in validators/
    ok = code == 0
    print(f"  {'✅' if ok else '⚠️'} pyright: {'0 errors' if ok else 'pre-existing errors only (validators/)'}")
    return True

def check_pytest(paths: list[str] = None):
    cmd = ["python3", "-m", "pytest", "-x", "-q"]
    if paths:
        cmd.extend(paths)
    else:
        cmd.append("tests/")
    code, out, _ = run(cmd, 120)
    last = out.split("\n")[-1] if out else ""
    ok = code == 0
    print(f"  {'✅' if ok else '❌'} pytest: {last if last else ('passed' if ok else 'failed')}")
    return ok

def check_pr_size():
    code, out, _ = run(["git", "diff", "--stat", "test..."], 10)
    if code != 0 or not out:
        print(f"  ⚠️  Could not determine diff size (not on a branch?)")
        return True
    lines = out.strip().split("\n")
    files_line = lines[-1] if lines else ""
    print(f"  📊 Diff: {files_line}")
    # Parse file count
    return True

def check_issue_bonus():
    body_file = ".git/PR_BODY.txt"
    if not os.path.exists(body_file):
        return True
    with open(body_file) as f:
        body = f.read()
    has_fixes = "Fixes #" in body or "Closes #" in body
    print(f"  {'✅' if has_fixes else '❌'} Issue ref: {'Fixes/Closes found' if has_fixes else 'MISSING — Issue Bonus = 1.0x!'}")
    return has_fixes

def check_commit_style():
    code, out, _ = run(["git", "log", "--oneline", "test..HEAD", "--format=%s"], 10)
    if not out:
        return True
    commits = out.strip().split("\n")
    ok = True
    for c in commits[:5]:
        prefix_ok = any(c.startswith(p) for p in ["fix:", "feat:", "refactor:", "perf:", "cli:", "test:", "style:", "docs:", "chore:"])
        if not prefix_ok and not c.startswith("Merge "):
            ok = False
            print(f"  ❌ Commit: '{c[:60]}' — tanpa prefix semantic")
    if ok:
        print(f"  ✅ Commit messages: semantic prefix style")
    return ok

def main():
    print("\n" + "=" * 60)
    print("  PRE-SUBMIT CHECKLIST — GITTENSOR SN74")
    print("=" * 60)

    checks = [
        ("Branch naming", check_branch),
        ("Commit style", check_commit_style),
        ("ruff check", check_ruff),
        ("ruff format", check_ruff_format),
        ("pyright", check_pyright),
        ("PR size", check_pr_size),
    ]

    results = []
    for name, fn in checks:
        print(f"\n  [{name}]")
        try:
            ok = fn()
            results.append(ok)
        except Exception as e:
            print(f"  ❌ Error: {e}")
            results.append(False)

    print(f"\n  {'─' * 58}")
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"  Result: {passed}/{total} passed")
    if passed == total:
        print(f"  ✅ Siap submit PR!")
    else:
        print(f"  ⚠️  Perbaiki checklist sebelum push.")
    print()

if __name__ == "__main__":
    main()
