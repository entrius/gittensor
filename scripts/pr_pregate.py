#!/usr/bin/env python3
"""
PR Pre-submit Gate — mandatory checks before PR creation.
Blocks PR if any check fails: label mismatch, low merge prob, bad body, scoring.

Usage:
  python3 scripts/pr_pregate.py --issue 123 --prefix fix --desc "handle null fields"
  python3 scripts/pr_pregate.py --issue 123 --prefix fix --desc "handle null fields" --json
  python3 scripts/pr_pregate.py --branch fix/123-handle-null-fields   # auto-detect
"""

import argparse, json, os, re, subprocess, sys, textwrap

REPO = "entrius/gittensor"
BASE = "test"
VALID_PREFIXES = ["fix", "feat"]
MIN_MERGE_PROB = 0.85
PREFIX_LABEL_MAP = {"fix": "bug", "feat": "enhancement"}

def run(cmd: list, timeout=30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        r = subprocess.CompletedProcess(cmd, -1, "", f"TIMEOUT ({timeout}s)")
        return r
    except FileNotFoundError:
        r = subprocess.CompletedProcess(cmd, -1, "", "command not found")
        return r

def check_label_prefix(prefix: str, title: str = "", branch: str = "") -> dict:
    expected_label = PREFIX_LABEL_MAP.get(prefix)
    if not expected_label:
        return {"ok": False, "msg": f"Prefix '{prefix}' tidak dikenal. Gunakan: {', '.join(VALID_PREFIXES)}", "fatal": True}

    clues = []
    if title and not title.startswith(f"{prefix}:"):
        clues.append(f"title '{title}' tidak mulai dengan '{prefix}:'")
    if branch and not branch.startswith(f"{prefix}/"):
        clues.append(f"branch '{branch}' tidak mulai dengan '{prefix}/'")

    ok = len(clues) == 0
    msg = f"Label prediksi: '{expected_label}' ({prefix}: → {expected_label})"
    if clues:
        msg += " ⚠️ " + "; ".join(clues)
    return {"ok": ok, "label": expected_label, "prefix": prefix, "msg": msg, "fatal": False}

def check_scoring(issue: int, prefix: str) -> dict:
    script = os.path.join(os.path.dirname(__file__), "pr_scoring_checklist.py")
    if not os.path.exists(script):
        script = os.path.join(os.path.dirname(__file__), "..", "scripts", "pr_scoring_checklist.py")
    if not os.path.exists(script):
        return {"ok": False, "msg": "pr_scoring_checklist.py tidak ditemukan", "fatal": True}

    r = run([sys.executable, script, "--issue", str(issue), "--prefix", prefix, "--json"], timeout=30)
    if r.returncode != 0 and r.returncode != 1:
        return {"ok": False, "msg": f"Scoring gagal: {r.stderr[:200]}", "fatal": False}

    try:
        checks = json.loads(r.stdout)
        failed = [c for c in checks if not c.get("ok")]
        ok = len(failed) == 0
        msg = f"Scoring: {len([c for c in checks if c.get('ok')])}/{len(checks)} ok"
        if failed:
            msg += " ❌ " + "; ".join(f.get("msg", "?")[:60] for f in failed)
        return {"ok": ok, "checks": len(checks), "failed": len(failed), "msg": msg, "fatal": False}
    except (json.JSONDecodeError, Exception) as e:
        return {"ok": False, "msg": f"Scoring parse error: {e}", "fatal": False}

def check_merge_prob(issue: int) -> dict:
    script = os.path.join(os.path.dirname(__file__), "pr_merge_predictor.py")
    if not os.path.exists(script):
        script = os.path.join(os.path.dirname(__file__), "..", "scripts", "pr_merge_predictor.py")
    if not os.path.exists(script):
        return {"ok": False, "msg": "pr_merge_predictor.py tidak ditemukan", "fatal": True}

    r = run([sys.executable, script, "--pr", str(issue), "--json"], timeout=30)
    if r.returncode != 0:
        return {"ok": True, "msg": "Merge predictor skip (PR belum ada)", "prob": None, "fatal": False}

    try:
        data = json.loads(r.stdout)
        prob = data.get("probability", 0)
        ok = prob >= MIN_MERGE_PROB
        tier = data.get("tier", "UNKNOWN")
        msg = f"Merge prob: {prob*100:.0f}% ({tier})"
        if not ok:
            msg += f" ❌ < {MIN_MERGE_PROB*100:.0f}% minimum"
        return {"ok": ok, "prob": prob, "msg": msg, "fatal": False}
    except (json.JSONDecodeError, Exception) as e:
        return {"ok": True, "msg": f"Merge predictor: {e} (skip)", "prob": None, "fatal": False}

def check_diff_quality() -> dict:
    script = os.path.join(os.path.dirname(__file__), "pr_diff_quality.py")
    if not os.path.exists(script):
        script = os.path.join(os.path.dirname(__file__), "..", "scripts", "pr_diff_quality.py")
    if not os.path.exists(script):
        return {"ok": True, "msg": "Diff quality skip (tool tidak ditemukan)", "fatal": False}

    r = run([sys.executable, script, "--branch", BASE, "--json"], timeout=30)
    if r.returncode != 0:
        return {"ok": True, "msg": "Diff quality skip (no diff)", "fatal": False}

    try:
        data = json.loads(r.stdout)
        score = data.get("score", 0)
        warnings = data.get("warnings", [])
        ok = score >= 0.5 and len(warnings) == 0
        msg = f"Diff quality: {score:.2f}"
        if warnings:
            msg += " ⚠️ " + "; ".join(w[:50] for w in warnings)
        return {"ok": ok, "score": score, "warnings": warnings, "msg": msg, "fatal": False}
    except (json.JSONDecodeError, Exception) as e:
        return {"ok": True, "msg": f"Diff quality: {e} (skip)", "fatal": False}

def check_body_format() -> dict:
    r = run(["git", "log", "-1", "--format=%B"], timeout=5)
    commit_msg = r.stdout.strip() if r.returncode == 0 else ""

    body_file = "/tmp/pr_body.txt"
    body = ""
    if os.path.exists(body_file):
        with open(body_file) as f:
            body = f.read()

    text = body or commit_msg
    if not text:
        return {"ok": True, "msg": "Body check: no body found (skip)", "fatal": False}

    has_summary = "## Summary" in text
    has_closes = bool(re.search(r'(?:Closes|Fixes|Resolves)\s+#\d+', text))
    has_validation = "## Validation" in text

    if has_summary and has_closes and has_validation:
        return {"ok": True, "msg": "Body format: merged-PR ✅", "fatal": False}
    if has_summary and has_closes:
        return {"ok": True, "msg": "Body format: minimal (Summary + Closes)", "fatal": False}

    missing = []
    if not has_summary: missing.append("## Summary")
    if not has_closes: missing.append("Closes #N")
    if not has_validation: missing.append("## Validation")
    return {"ok": False, "msg": "Body format ❌ missing: " + ", ".join(missing), "fatal": False}

def run_gate(issue: int, prefix: str, title: str = "", branch: str = "", desc: str = "") -> list:
    checks = []
    checks.append(check_label_prefix(prefix, title, branch))
    if issue:
        checks.append(check_scoring(issue, prefix))
        checks.append(check_merge_prob(issue))
    checks.append(check_diff_quality())
    # Body check only if there's a commit body
    r = run(["git", "log", "-1", "--format=%B"], timeout=5)
    if r.returncode == 0 and r.stdout.strip():
        checks.append(check_body_format())
    return checks

def print_results(checks: list, prefix_str: str = "  ") -> bool:
    all_ok = True
    fatal = False
    print(f"\n{prefix_str}{'='*55}")
    print(f"{prefix_str}  PR PRE-SUBMIT GATE")
    print(f"{prefix_str}{'='*55}")
    for c in checks:
        if c.get("fatal"):
            mark = "💀"
            fatal = True
        elif c.get("ok"):
            mark = "✅"
        else:
            mark = "❌"
            all_ok = False
        print(f"{prefix_str}{mark}  {c['msg']}")
    print(f"{prefix_str}{'='*55}")
    if fatal:
        print(f"{prefix_str}  💀 FATAL — perbaiki konfigurasi")
    elif all_ok:
        print(f"{prefix_str}  ✅ SEMUA AMAN — siap buat PR")
    else:
        print(f"{prefix_str}  ❌ GAGAL — perbaiki sebelum buat PR")
    print(f"{prefix_str}{'='*55}")
    return all_ok and not fatal

def main():
    p = argparse.ArgumentParser(
        description="PR Pre-submit Gate — mandatory checks sebelum buat PR",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s --issue 123 --prefix fix
              %(prog)s --issue 123 --prefix fix --desc "handle null fields"
              %(prog)s --branch fix/123-handle-null
              %(prog)s --json
        """),
    )
    p.add_argument("--issue", type=int, help="Issue/PR number")
    p.add_argument("--prefix", default="fix", help=f"Prefix: {', '.join(VALID_PREFIXES)}")
    p.add_argument("--title", help="PR title (auto-detect if not set)")
    p.add_argument("--desc", help="Description (for context)")
    p.add_argument("--branch", help="Branch name (auto-detect prefix from branch)")
    p.add_argument("--json", action="store_true", help="JSON output")
    args = p.parse_args()

    prefix = args.prefix
    title = args.title or ""
    branch = args.branch or ""

    # Auto-detect prefix from branch name
    if args.branch and not args.prefix:
        m = re.match(r'(fix|feat)/', args.branch)
        if m:
            prefix = m.group(1)

    # Auto-detect title from branch
    if not title and branch:
        slug = branch.split("/", 1)[-1] if "/" in branch else branch
        title = f"{prefix}: {slug.replace('-', ' ')}"

    issue = args.issue

    checks = run_gate(
        issue=issue,
        prefix=prefix,
        title=title,
        branch=branch,
        desc=args.desc or "",
    )

    if args.json:
        output = {
            "gate": "pr_pregate",
            "passed": all(c.get("ok") for c in checks if not c.get("fatal")),
            "fatal": any(c.get("fatal") for c in checks),
            "checks": checks,
            "issue": issue,
            "prefix": prefix,
        }
        print(json.dumps(output, indent=2))
        sys.exit(0 if output["passed"] and not output["fatal"] else 1)
    else:
        ok = print_results(checks)
        sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
