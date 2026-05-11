#!/usr/bin/env python3
"""
PR Diff Summary — auto-generate ## Summary dari git diff.

Usage:
  python3 scripts/pr_diff_summary.py                              # branch aktif vs test
  python3 scripts/pr_diff_summary.py --branch fix/123-handle-null
  python3 scripts/pr_diff_summary.py --base test
  python3 scripts/pr_diff_summary.py --json
  python3 scripts/pr_diff_summary.py --format body                # Output full body block
"""

import argparse, json, os, re, subprocess, sys, textwrap

BASE = "test"

def run(cmd: list, timeout=15) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, -1, "", f"TIMEOUT")

def get_diff_files(base: str) -> list:
    r = run(["git", "diff", "--name-only", f"{base}..."], timeout=10)
    return [f.strip() for f in r.stdout.strip().split("\n") if f.strip()] if r.returncode == 0 else []

def get_diff_stat(base: str) -> str:
    r = run(["git", "diff", "--stat", f"{base}..."], timeout=10)
    return r.stdout.strip() if r.returncode == 0 else ""

def classify_files(files: list) -> dict:
    categories = {
        "bugfix": ["fix", "bug", "error", "crash", "fail", "null", "edge"],
        "refactor": ["refactor", "rename", "clean", "move", "restruct"],
        "test": ["test_", "spec_", "conftest"],
        "config": [".json", ".yaml", ".yml", ".toml", ".cfg", "config"],
        "docs": [".md", "docs/", "doc/", "readme"],
        "cli": ["cli", "command", "argparse"],
    }

    classifications = set()
    for f in files:
        ext = os.path.splitext(f)[1]
        for cat, keywords in categories.items():
            if any(k in f.lower() or k in ext for k in keywords):
                classifications.add(cat)
    return classifications

def generate_summary(base: str = BASE, issue: int = None, desc: str = "") -> dict:
    files = get_diff_files(base)
    stat = get_diff_stat(base)
    categories = classify_files(files)

    n_files = len(files)
    lines_changed = 0
    m = re.search(r'(\d+) insertions', stat)
    if m:
        lines_changed = int(m.group(1))
    m = re.search(r'(\d+) deletions', stat)
    if m:
        lines_changed += int(m.group(1))

    if not files:
        return {"ok": False, "summary": "", "files": [], "msg": "No diff found"}

    # Build narrative
    parts = []
    if desc:
        parts.append(desc)
    elif categories:
        if "bugfix" in categories:
            parts.append("Fix bug")
        elif "refactor" in categories:
            parts.append("Refactor")
        else:
            parts.append("Update")
        parts.append(f"di {n_files} file")
    else:
        parts.append(f"Changes di {n_files} file")

    if "test" in categories:
        parts.append("(termasuk test)")
    if "config" in categories:
        parts.append("(ubah konfigurasi)")
    if "cli" in categories:
        parts.append("(CLI change)")

    # File list
    file_list = "\n".join(f"  - `{f}`" for f in files[:15])
    if len(files) > 15:
        file_list += f"\n  - ... +{len(files) - 15} files"

    summary = f"{' '.join(parts)}:\n\n{file_list}"

    if lines_changed:
        summary += f"\n\nTotal: +{lines_changed} lines"

    if issue:
        summary += f"\n\nCloses #{issue}"

    return {
        "ok": True,
        "summary": summary,
        "files": files,
        "n_files": n_files,
        "lines_changed": lines_changed,
        "categories": list(categories),
        "msg": f"Summary generated: {n_files} files, ~{lines_changed} lines"
    }

def main():
    p = argparse.ArgumentParser(
        description="PR Diff Summary — auto-generate Summary dari git diff",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s
              %(prog)s --branch fix/123-handle-null
              %(prog)s --issue 123 --desc "Handle null fields"
              %(prog)s --json
              %(prog)s --format body    # Full ## Summary block
        """),
    )
    p.add_argument("--branch", help="Branch name (auto-detect from HEAD)")
    p.add_argument("--base", default=BASE, help=f"Base branch (default: {BASE})")
    p.add_argument("--issue", type=int, help="Issue number for Closes #N")
    p.add_argument("--desc", default="", help="Override description (opsional)")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--format", choices=["text", "body"], default="text",
                    help="Output format: text or full body block (default: text)")
    args = p.parse_args()

    result = generate_summary(base=args.base, issue=args.issue, desc=args.desc)

    if args.json:
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    if not result.get("ok"):
        print(f"  No diff found against '{args.base}'")
        sys.exit(1)

    if args.format == "body":
        print(f"\n## Summary\n\n{result['summary']}\n")
    else:
        print(f"\n{result['summary']}\n")
        print(f"  ({result['n_files']} files, ~{result['lines_changed']} lines)")
        if result.get("categories"):
            print(f"  Categories: {', '.join(result['categories'])}")

if __name__ == "__main__":
    main()
