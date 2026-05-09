#!/usr/bin/env python3
"""
Capture real terminal output and embed as code blocks in PR body.
Ganti Pillow screenshot dengan terminal output asli — lebih dipercaya reviewer.

Usage:
  python3 capture_terminal.py --cmd "gitt miner check --network finney" --label "Before fix"
  python3 capture_terminal.py --cmd "gitt miner post --network finney" --label "After fix"
  python3 capture_terminal.py --all --pr-dir ./pr_evidence/
"""

import argparse, subprocess, sys, os, shutil, json
from datetime import datetime

CAPTURE_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "terminal_captures")

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

def run_cmd(cmd: str, timeout: int = 30) -> tuple[str, str, int]:
    """Run a shell command, return (stdout, stderr, returncode)."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", f"TIMEOUT after {timeout}s", -1
    except FileNotFoundError as e:
        return "", f"COMMAND NOT FOUND: {e}", -1

def generate_code_block(label: str, stdout: str, stderr: str, returncode: int) -> str:
    """Generate a markdown code block with labeled terminal output."""
    parts = [f"### {label}\n\n```"]
    if stdout:
        parts.append(stdout.rstrip("\n"))
    if stderr:
        if stdout:
            parts.append("")
        parts.append(stderr.rstrip("\n"))
    parts.append(f"```\n*Exit code: {returncode}*\n")
    return "\n".join(parts)

def capture_and_save(cmd: str, label: str, pr_num: int = None, save_file: bool = True) -> str:
    """Run command, capture output, return markdown block. Optionally save to file."""
    stdout, stderr, rc = run_cmd(cmd)
    block = generate_code_block(label, stdout, stderr, rc)

    if save_file and pr_num:
        ensure_dir(CAPTURE_DIR)
        slug = label.lower().replace(" ", "_").replace("/", "_")[:40]
        fname = f"pr{pr_num}_{slug}.md"
        fpath = os.path.join(CAPTURE_DIR, fname)
        with open(fpath, "w") as f:
            f.write(block)
        print(f"  💾 Saved: {fpath}")

    return block

def capture_all_for_pr(pr_num: int, before_cmd: str, after_cmd: str):
    """Capture before and after for a CLI fix PR."""
    blocks = []
    print(f"\n  📸 Capturing terminal evidence for PR #{pr_num}")
    blocks.append(capture_and_save(before_cmd, "Before fix (CLI output)", pr_num))
    print(f"  ✅ Before captured")
    blocks.append(capture_and_save(after_cmd, "After fix (CLI output)", pr_num))
    print(f"  ✅ After captured")

    # Combine into a single evidence block
    evidence = "\n".join(blocks)
    evidence_path = os.path.join(CAPTURE_DIR, f"pr{pr_num}_evidence.md")
    with open(evidence_path, "w") as f:
        f.write(evidence)
    print(f"  💾 Combined evidence: {evidence_path}")
    return evidence

def verify_commands_exist(commands: list[str]) -> bool:
    """Check that all commands exist before running."""
    all_ok = True
    for cmd in commands:
        base = cmd.split()[0]
        if not shutil.which(base):
            print(f"  ⚠️  Command not found: {base}")
            all_ok = False
    return all_ok

def main():
    p = argparse.ArgumentParser(description="Capture terminal output for PR evidence")
    p.add_argument("--cmd", help="Single command to run")
    p.add_argument("--label", default="Terminal output", help="Label for the block")
    p.add_argument("--pr", type=int, default=None, help="PR number for file naming")
    p.add_argument("--before", help="Command showing BEFORE fix (CLI changes)")
    p.add_argument("--after", help="Command showing AFTER fix (CLI changes)")
    p.add_argument("--all", action="store_true", help="Run all commands in pr_evidence.json")
    p.add_argument("--no-save", action="store_true", help="Print block to stdout only, no file")
    p.add_argument("--output", help="Write markdown block to this file")
    args = p.parse_args()

    if args.all:
        # Load evidence config
        config_path = os.path.join(os.path.dirname(__file__), "pr_evidence.json")
        if not os.path.exists(config_path):
            print(f"❌ No pr_evidence.json found at {config_path}")
            sys.exit(1)
        with open(config_path) as f:
            evidence_config = json.load(f)

        for entry in evidence_config.get("captures", []):
            pr_num = entry.get("pr")
            if entry.get("before") and entry.get("after"):
                if not verify_commands_exist([entry["before"], entry["after"]]):
                    print(f"  ⚠️  Skipping PR #{pr_num} — missing commands")
                    continue
                capture_all_for_pr(pr_num, entry["before"], entry["after"])
            elif entry.get("cmd"):
                block = capture_and_save(entry["cmd"], entry.get("label", "Terminal output"), pr_num)
                print(block)
        return

    if args.before and args.after:
        if args.pr:
            capture_all_for_pr(args.pr, args.before, args.after)
        else:
            print(capture_and_save(args.before, "Before fix"))
            print(capture_and_save(args.after, "After fix"))
        return

    if args.cmd:
        block = capture_and_save(args.cmd, args.label, args.pr, not args.no_save)
        if args.output:
            with open(args.output, "w") as f:
                f.write(block)
            print(f"  💾 Written to: {args.output}")
        else:
            print(block)
        return

    p.print_help()

if __name__ == "__main__":
    main()
