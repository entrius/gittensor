#!/usr/bin/env python3
"""
PR Diff Quality — Analyze diff quality before committing.

Scores the diff against merged-PR patterns so you can fix issues
BEFORE the maintainer sees them.  Factors checked:
  - Total lines changed (target <100)
  - Files touched (target <=4)
  - Code density (token_score / total_lines)
  - Test presence (files containing 'test')
  - Config-only PR warning (only .json/.yaml/.md)
  - Binary file warning

Usage:
  python3 pr_diff_quality.py                          # Analyze working tree
  python3 pr_diff_quality.py --branch fix/123-slug    # Analyze a branch vs test
  python3 pr_diff_quality.py --json                   # Machine-readable output
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.basename(REPO) == "opencode":
    REPO = os.path.join(REPO, "gittensor")

RUFF_WEIGHT = {
    ".py": 1.0,
    ".rs": 1.2,
    ".ts": 0.9,
    ".js": 0.8,
    ".go": 1.0,
    ".tsx": 0.9,
    ".jsx": 0.8,
    ".json": 0.12,
    ".yaml": 0.12,
    ".yml": 0.12,
    ".md": 0.12,
}

MAX_LINES = 100
MAX_FILES = 4
TEST_WEIGHT = 0.05


def run_git(args, repo=REPO):
    cmd = ["git"] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=repo)
    return r


def get_branch_diff(branch):
    r = run_git(["diff", f"test...{branch}", "--stat"])
    if r.returncode != 0:
        r = run_git(["diff", "test.." + branch, "--stat"])
    return r.stdout


def get_working_diff():
    r = run_git(["diff", "--stat"])
    staged = run_git(["diff", "--cached", "--stat"])
    return r.stdout + staged.stdout


def parse_diffstat(text):
    files = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or "file changed" in line or "files changed" in line:
            continue
        m = re.match(r"(.+?)\s+\|", line)
        if m:
            fname = m.group(1).strip()
            files.append(fname)
    return files


def count_lines(text):
    total = 0
    for line in text.strip().split("\n"):
        m = re.search(r"(\d+) insertion", line)
        if m:
            total += int(m.group(1))
        m = re.search(r"(\d+) deletion", line)
        if m:
            total += int(m.group(1))
    return total


def estimate_token_score(files):
    total = 0
    for f in files:
        ext = Path(f).suffix
        w = RUFF_WEIGHT.get(ext, 0.5)
        weight = TEST_WEIGHT if "test" in f.lower() else w
        total += weight * 100
    return round(total, 2)


def check_binary_files(files):
    return [f for f in files if Path(f).suffix in {".png", ".jpg", ".ico", ".pdf", ".whl", ".so", ".dll"}]


def check_config_only(files):
    config_exts = {".json", ".yaml", ".yml", ".md", ".toml", ".cfg", ".ini"}
    if not files:
        return False
    return all(Path(f).suffix in config_exts for f in files)


def check_no_test_files(files):
    return not any("test" in f.lower() for f in files)


def analyze(text, json_output=False):
    files = parse_diffstat(text)
    total_lines = count_lines(text)
    n_files = len(files)
    token_score = estimate_token_score(files)
    code_density = round(min(token_score / max(total_lines, 1), 1.5), 3)
    binaries = check_binary_files(files)
    config_only = check_config_only(files)
    no_tests = check_no_test_files(files)

    issues = []
    if total_lines > MAX_LINES:
        issues.append(f"TOO MANY LINES: {total_lines} (max {MAX_LINES})")
    if n_files > MAX_FILES:
        issues.append(f"TOO MANY FILES: {n_files} (max {MAX_FILES})")
    if binaries:
        issues.append(f"BINARY FILES: {binaries}")
    if config_only:
        issues.append("CONFIG-ONLY: no code changes")
    if no_tests:
        issues.append("NO TEST FILES: consider adding tests")
    if code_density < 0.3:
        issues.append(f"LOW DENSITY: {code_density} (target >= 0.3)")

    estimated_label_mult = 1.1
    estimated_repo_weight = 1.0
    estimated_issue_mult = 1.33
    estimated_score = round(
        25 * code_density * estimated_repo_weight * estimated_issue_mult * estimated_label_mult,
        2,
    )

    result = {
        "files_changed": n_files,
        "lines_changed": total_lines,
        "code_density": code_density,
        "estimated_token_score": token_score,
        "estimated_base_score": estimated_score,
        "has_tests": not no_tests,
        "config_only": config_only,
        "binary_files": binaries,
        "issues": issues,
        "verdict": "PASS" if not issues else "WARN" if len(issues) <= 2 else "FAIL",
    }

    if json_output:
        print(json.dumps(result, indent=2))
        return

    print(f"\n{'='*50}")
    print(f"  PR DIFF QUALITY REPORT")
    print(f"{'='*50}")
    print(f"  Files:     {n_files:>3} (target <= {MAX_FILES})")
    print(f"  Lines:     {total_lines:>3} (target <= {MAX_LINES})")
    print(f"  Density:   {code_density:>.3f} (target >= 0.3)")
    print(f"  Token:     {token_score:>.1f}")
    print(f"  Est Score: {estimated_score:>.2f}")
    print(f"  Tests:     {'YES' if not no_tests else 'NO'}")
    if config_only:
        print(f"  ⚠️  Config-only change")
    if binaries:
        print(f"  ❌ Binary files: {binaries}")
    print(f"{'='*50}")
    if issues:
        for issue in issues:
            print(f"  ❌ {issue}")
    else:
        print(f"  ✅ No issues found")
    print(f"  VERDICT: {result['verdict']}")
    print(f"{'='*50}\n")


def main():
    ap = argparse.ArgumentParser(description="PR Diff Quality Analyzer")
    ap.add_argument("--branch", "-b", help="Branch to analyze (default: working tree)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args()

    if args.branch:
        text = get_branch_diff(args.branch)
    else:
        text = get_working_diff()

    if not text.strip():
        print("No diff found.")
        sys.exit(0)

    analyze(text, json_output=args.json)


if __name__ == "__main__":
    main()
