#!/usr/bin/env python3
"""
PR Flow — Flexible PR workflow orchestrator.

Configurable pipeline: validate → branch → commit → push → create_pr → add_reviewers → evidence
Supports single PRs, batch mode, dry-run, resume, custom body styles, and any repo.

Usage:
  python3 scripts/pr_flow.py --issue 123 --prefix fix                          # Quick single PR
  python3 scripts/pr_flow.py --config pr_config.yaml                          # From config file
  python3 scripts/pr_flow.py --batch pr_batch.json                            # Batch mode
  python3 scripts/pr_flow.py --issue 123 --dry-run                             # Preview only
  python3 scripts/pr_flow.py --resume                                          # Resume failed run
  python3 scripts/pr_flow.py --list-presets                                    # Show body style presets
  python3 scripts/pr_flow.py --issue 123 --steps validate,commit,push --no-pr  # Partial pipeline
"""

import argparse, json, os, re, subprocess, sys, textwrap, time, uuid
from datetime import datetime, timezone
from pathlib import Path

# ── DEFAULT CONFIG ────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "repo": "entrius/gittensor",
    "fork": "alpurkan17/gittensor",
    "base": "test",
    "reviewers": ["anderdc", "landyndev"],
    "body_style": "v3-full",
    "branch_pattern": "{prefix}/{issue}-{slug}",
    "step_timeout": 30,
    "steps": ["validate", "branch", "commit", "push", "pregate", "create_pr", "add_reviewers"],
    "hooks": {
        "pre_validate": None,
        "post_validate": None,
        "pre_commit": None,
        "post_commit": None,
        "pre_push": None,
        "post_push": None,
        "pre_create": None,
        "post_create": None,
    },
    "evidence": {
        "capture": False,
        "output_dir": "terminal_captures",
    },
    "valid_prefixes": ["fix", "feat"],
}

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "pr_config.yaml")
STATE_FILE = "/tmp/pr_flow_state.json"

PRESETS = {
    "v3-full": {
        "sections": ["summary", "root_cause", "impact", "why", "related", "cross_refs",
                      "test_plan", "live_verif", "terminal_evidence", "how_to_verify",
                      "edge_cases", "out_of_scope", "post_merge"],
    },
}

PREFIX_TO_LABEL = {
    "fix": "bug",
    "feat": "enhancement",
    "perf": "enhancement",
    "cli": "enhancement",
    "refactor": "refactor",
    "docs": "documentation",
    "test": "bug",
    "style": "refactor",
}


# ── HELPERS ────────────────────────────────────────────────────────────────────

def log(msg, level="info"):
    icons = {"info": "→", "ok": "✅", "warn": "⚠️", "err": "❌", "dry": "🔍"}
    icon = icons.get(level, "·")
    print(f"  {icon} {msg}")


def run(cmd: list, timeout=30, check=False, capture=True):
    try:
        r = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)
        if check and r.returncode != 0:
            log(f"Exit {r.returncode}: {r.stderr[:200]}", "warn")
        return r
    except subprocess.TimeoutExpired:
        log(f"Timeout ({timeout}s)", "err")
        return None
    except FileNotFoundError:
        log(f"Command not found: {cmd[0]}", "err")
        return None


def make_slug(title: str, max_len=40) -> str:
    slug = title.lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = slug.strip('-')
    slug = slug[:max_len].rstrip('-')
    return slug


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def body_has_section(body: str, section: str) -> bool:
    return re.search(rf'^##\s*{section}', body, re.MULTILINE | re.IGNORECASE) is not None


# ── CONFIG MANAGEMENT ──────────────────────────────────────────────────────────

def load_config(config_path: str = None) -> dict:
    cfg = dict(DEFAULT_CONFIG)

    # Try explicit path
    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            if config_path.endswith(".yaml") or config_path.endswith(".yml"):
                try:
                    import yaml
                    user_cfg = yaml.safe_load(f)
                except ImportError:
                    log("PyYAML not installed, using JSON fallback", "warn")
                    user_cfg = json.load(f)
            else:
                user_cfg = json.load(f)
        if user_cfg:
            _deep_merge(cfg, user_cfg)
        log(f"Loaded config: {config_path}", "ok")
        return cfg

    # Try default paths
    for p in [CONFIG_PATH, "pr_config.json", "pr_config.yaml"]:
        if os.path.exists(p):
            return load_config(p)

    log("Using default config (no config file found)", "info")
    return cfg


def _deep_merge(base, override):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ── STATE MANAGEMENT (for resume) ──────────────────────────────────────────────

def save_state(state: dict):
    state["_updated"] = now_iso()
    save_json(STATE_FILE, state)


def load_state() -> dict | None:
    return load_json(STATE_FILE)


def clear_state():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)


# ── PIPELINE STEPS ─────────────────────────────────────────────────────────────

class StepContext:
    """Context passed through pipeline steps."""
    def __init__(self, config: dict, issue: int = None, prefix: str = "fix",
                 title: str = "", desc: str = "", slug: str = "",
                 branch: str = "", body: str = "", dry_run: bool = False,
                 files: list = None, **kwargs):
        self.config = config
        self.issue = issue
        self.prefix = prefix
        self.title = title
        self.desc = desc
        self.slug = slug
        self.branch = branch
        self.body = body
        self.dry_run = dry_run
        self.files = files or []
        self.pr_number = None
        self.pr_url = None
        self.committed = False
        self.pushed = False
        self.created = False
        self.failed_step = None
        self._extra = kwargs

    def to_dict(self):
        return {
            "issue": self.issue, "prefix": self.prefix,
            "title": self.title, "desc": self.desc, "slug": self.slug,
            "branch": self.branch, "body": self.body, "dry_run": self.dry_run,
            "files": self.files, "pr_number": self.pr_number, "pr_url": self.pr_url,
            "committed": self.committed, "pushed": self.pushed, "created": self.created,
            "failed_step": self.failed_step,
        }

    @classmethod
    def from_dict(cls, d, config):
        ctx = cls(config=config, **{k: v for k, v in d.items() if k != "dry_run"})
        ctx.dry_run = d.get("dry_run", False)
        return ctx


def step_validate(ctx: StepContext):
    """Run pre-submit validation."""
    log("Validating code...")

    hooks = ctx.config.get("hooks", {})
    pre = hooks.get("pre_validate")
    if pre:
        log(f"Pre-validate hook: {pre}")
        if not ctx.dry_run:
            run(pre.split(), timeout=60, check=False)

    checks = [
        ("ruff check", ["ruff", "check", "."]),
        ("ruff format", ["ruff", "format", "--check", "."]),
    ]

    all_ok = True
    for name, cmd in checks:
        log(f"[{name}]...", "info")
        if ctx.dry_run:
            log(f"Would run: {' '.join(cmd)}", "dry")
            continue
        r = run(cmd, timeout=60, check=False)
        ok = r and r.returncode == 0
        log(f"{name}: {'PASS' if ok else 'FAIL'}", "ok" if ok else "warn")
        if not ok:
            all_ok = False

    if not all_ok and ctx.config.get("strict_validation", True):
        log("Fix lint errors first, or use --loose to skip", "err")
        ctx.failed_step = "validate"
        return False

    post = hooks.get("post_validate")
    if post and not ctx.dry_run:
        run(post.split(), timeout=60, check=False)

    return True


def step_branch(ctx: StepContext):
    """Create branch from base."""
    branch = ctx.branch or f"{ctx.prefix}/{ctx.issue}-{ctx.slug}"
    ctx.branch = branch

    log(f"Branch: {branch}")

    if ctx.dry_run:
        log(f"Would create branch '{branch}' from '{ctx.config['base']}'", "dry")
        return True

    # Check if exists
    r = run(["git", "rev-parse", "--verify", branch], timeout=5, check=False)
    if r and r.returncode == 0:
        log(f"Branch exists, switching...", "info")
        run(["git", "checkout", branch], timeout=5)
        return True

    base = ctx.config["base"]
    run(["git", "checkout", base], timeout=5)
    run(["git", "pull", "origin", base], timeout=15)
    run(["git", "checkout", "-b", branch], timeout=5)
    log(f"Branch created from '{base}'", "ok")
    return True


def step_commit(ctx: StepContext):
    """Stage and commit changes."""
    if ctx.dry_run:
        log(f"Would commit: {ctx.prefix}: {ctx.desc} (#{ctx.issue})", "dry")
        ctx.committed = True
        return True

    hooks = ctx.config.get("hooks", {})
    pre = hooks.get("pre_commit")
    if pre:
        run(pre.split(), timeout=60, check=False)

    run(["git", "add", "-A"], timeout=10)

    r = run(["git", "diff", "--cached", "--quiet"], timeout=5, check=False)
    if r and r.returncode == 0:
        log("Nothing to commit", "warn")
        ctx.committed = False
        return True

    msg = f"{ctx.prefix}: {ctx.desc} (#{ctx.issue})"
    r = run(["git", "commit", "-m", msg], timeout=10, check=False)
    if r and r.returncode == 0:
        log(f"Committed: {msg}", "ok")
        ctx.committed = True
    else:
        stderr = r.stderr[:200] if r else "unknown error"
        log(f"Commit failed: {stderr}", "err")
        ctx.failed_step = "commit"
        return False

    post = hooks.get("post_commit")
    if post:
        run(post.split(), timeout=60, check=False)

    return True


def step_push(ctx: StepContext):
    """Push branch to fork."""
    if ctx.dry_run:
        log(f"Would push origin/{ctx.branch}", "dry")
        ctx.pushed = True
        return True

    hooks = ctx.config.get("hooks", {})
    pre = hooks.get("pre_push")
    if pre:
        run(pre.split(), timeout=60, check=False)

    r = run(["git", "push", "-u", "origin", ctx.branch], timeout=60, check=False)
    if r and r.returncode == 0:
        log(f"Pushed origin/{ctx.branch}", "ok")
        ctx.pushed = True
    else:
        stderr = r.stderr[:200] if r else "unknown error"
        log(f"Push failed: {stderr}", "err")

        # Retry with explicit remote URL
        fork = ctx.config["fork"]
        log("Retrying with explicit remote...", "info")
        r2 = run(["git", "push", "-u", "origin", ctx.branch, "--force"], timeout=60, check=False)
        if r2 and r2.returncode == 0:
            log(f"Pushed (force) origin/{ctx.branch}", "ok")
            ctx.pushed = True
        else:
            ctx.failed_step = "push"
            return False

    post = hooks.get("post_push")
    if post:
        run(post.split(), timeout=60, check=False)

    return True


def step_create_pr(ctx: StepContext):
    """Create PR on upstream repo."""
    if ctx.dry_run:
        log(f"Would create PR: {ctx.title}", "dry")
        log(f"Body preview:\n{ctx.body[:500]}...", "dry")
        ctx.created = True
        return True

    hooks = ctx.config.get("hooks", {})
    pre = hooks.get("pre_create")
    if pre:
        run(pre.split(), timeout=60, check=False)

    fork_owner = ctx.config["fork"].split("/")[0]
    head = f"{fork_owner}:{ctx.branch}"
    repo = ctx.config["repo"]
    base = ctx.config["base"]

    # Write body to file to avoid shell issues
    body_file = "/tmp/pr_flow_body.md"
    with open(body_file, "w") as f:
        f.write(ctx.body)

    r = run([
        "gh", "pr", "create",
        "--repo", repo,
        "--base", base,
        "--head", head,
        "--title", ctx.title,
        "--body-file", body_file,
    ], timeout=30, check=False)

    if r and r.returncode == 0:
        url = r.stdout.strip()
        m = re.search(r'#(\d+)', url)
        ctx.pr_number = int(m.group(1)) if m else None
        ctx.pr_url = url
        log(f"PR #{ctx.pr_number}: {url}", "ok")
        ctx.created = True
    else:
        # Check if PR already exists
        stderr = r.stderr if r else ""
        m = re.search(r'pull/(\d+)', stderr)
        if m:
            ctx.pr_number = int(m.group(1))
            ctx.pr_url = f"https://github.com/{repo}/pull/{ctx.pr_number}"
            log(f"PR already exists at #{ctx.pr_number}", "warn")
            ctx.created = True
        else:
            log(f"PR creation failed: {stderr[:300]}", "err")
            ctx.failed_step = "create_pr"
            return False

    post = hooks.get("post_create")
    if post:
        run(post.split(), timeout=60, check=False)

    return True


def step_add_reviewers(ctx: StepContext):
    """Request reviewers (may fail on fork)."""
    if ctx.dry_run:
        log(f"Would add reviewers: {ctx.config['reviewers']}", "dry")
        return True

    if not ctx.pr_number:
        log("No PR number, skipping reviewers", "warn")
        return True

    repo = ctx.config["repo"]
    for reviewer in ctx.config.get("reviewers", []):
        r = run([
            "gh", "api",
            f"repos/{repo}/pulls/{ctx.pr_number}/requested_reviewers",
            "--method", "POST",
            "-f", f"reviewers[]={reviewer}",
        ], timeout=15, check=False)
        if r and r.returncode == 0:
            log(f"Reviewer @{reviewer} requested", "ok")
        else:
            log(f"Can't add @{reviewer} from fork (expected)", "warn")

    return True


def step_evidence(ctx: StepContext):
    """Capture terminal evidence for CLI changes."""
    if not ctx.config.get("evidence", {}).get("capture"):
        log("Evidence capture disabled", "info")
        return True

    if ctx.dry_run:
        log("Would capture terminal evidence", "dry")
        return True

    log("Capturing terminal evidence...", "info")
    output_dir = ctx.config["evidence"].get("output_dir", "terminal_captures")
    os.makedirs(output_dir, exist_ok=True)

    evidence_file = os.path.join(output_dir, f"pr{ctx.pr_number or ctx.issue}_evidence.md")
    log(f"  Evidence file: {evidence_file}", "info")
    # Placeholder — actual capture requires knowing what commands to run
    log("  (run capture_terminal.py separately for real evidence)", "warn")

    return True


# ── BODY GENERATION ────────────────────────────────────────────────────────────

def build_body(ctx: StepContext) -> str:
    """Generate PR body using pr_body_builder."""
    from pr_body_builder import build as _build_body

    rc = ctx._extra.get("root_cause", "")
    imp = ctx._extra.get("impact", "")
    sol = ctx.desc.split("\n") if ctx.desc else [f"fix issue #{ctx.issue}"]
    tests = ctx._extra.get("tests", None) or ["ruff check", "ruff format --check"]
    htv = ctx._extra.get("how_to_verify", [])
    ec = ctx._extra.get("edge_cases", [])
    pm = ctx._extra.get("post_merge", [])
    why = ctx._extra.get("why", "")
    cross = ctx._extra.get("cross_refs", [])
    oos = ctx._extra.get("out_of_scope", "")

    return _build_body(
        issue=ctx.issue,
        title=ctx.title,
        root_cause=rc,
        impact=imp,
        solution=sol,
        tests=tests,
        live_verif=ctx._extra.get("live_verif", None),
        why=why,
        how_to_verify=htv if htv else None,
        post_merge=pm if pm else None,
        edge_cases=ec if ec else None,
        cross_refs=cross if cross else None,
        out_of_scope=oos,
    )


# ── AUTO-DETECT ────────────────────────────────────────────────────────────────

def auto_detect(ctx: StepContext):
    """Auto-detect changes and classify PR."""
    r = run(["git", "diff", "--name-only", f"{ctx.config['base']}..."], timeout=10, check=False)
    if r and r.stdout.strip():
        ctx.files = r.stdout.strip().split("\n")

    r2 = run(["git", "diff", "--stat", f"{ctx.config['base']}..."], timeout=10, check=False)
    ctx._extra["diff_stat"] = r2.stdout.strip() if r2 else ""

    has_cli = any("cli" in f or "miner_command" in f for f in ctx.files)
    has_test = any("test_" in f for f in ctx.files)
    ctx._extra["has_cli"] = has_cli
    ctx._extra["has_test"] = has_test


# ── PIPELINE ORCHESTRATOR ──────────────────────────────────────────────────────

def step_pregate(ctx: StepContext):
    """Mandatory pre-submit gate — blocks PR if checks fail."""
    log("Running pre-submit gate...", "info")
    if ctx.dry_run:
        log("Would run: pr_pregate.py", "dry")
        return True

    script = os.path.join(os.path.dirname(__file__), "pr_pregate.py")
    if not os.path.exists(script):
        script = os.path.join(os.path.dirname(__file__), "scripts", "pr_pregate.py")
    if not os.path.exists(script):
        log("pr_pregate.py not found — skipping gate", "warn")
        return True

    cmd = [sys.executable, script, "--json"]
    if ctx.issue:
        cmd += ["--issue", str(ctx.issue)]
    cmd += ["--prefix", ctx.prefix]
    if ctx.title:
        cmd += ["--title", ctx.title]
    if ctx.branch:
        cmd += ["--branch", ctx.branch]
    if ctx.desc:
        cmd += ["--desc", ctx.desc]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    if r.returncode != 0:
        try:
            result = json.loads(r.stdout)
            if result.get("fatal"):
                log("Gate FATAL — configuration error", "err")
                ctx.failed_step = "pregate"
                return False
            log("Gate FAILED — perbaiki sebelum create PR", "err")
            for c in result.get("checks", []):
                if not c.get("ok"):
                    log(f"  ❌ {c.get('msg', '?')}", "err")
            ctx.failed_step = "pregate"
            return False
        except (json.JSONDecodeError, ValueError):
            log("Gate FAILED (unknown error)", "err")
            log(r.stderr[:300], "err")
            ctx.failed_step = "pregate"
            return False

    log("Gate PASSED — semua aman ✅", "ok")
    return True


def step_scoring(ctx: StepContext):
    """Run PR scoring checklist before creation."""
    header("SCORING CHECKLIST")
    issue = ctx.issue
    prefix = ctx.prefix
    r = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__), "pr_scoring_checklist.py"),
         "--issue", str(issue), "--prefix", prefix, "--json"],
        capture_output=True, text=True, timeout=30
    )
    if r.returncode != 0:
        try:
            checks = json.loads(r.stdout)
            all_ok = all(c.get("ok", False) for c in checks)
        except (json.JSONDecodeError, ValueError):
            all_ok = False
        if all_ok:
            return True
        log("Scoring checklist FAILED", "warn")
        log(r.stdout, "info")
        try:
            ok = input("  Scoring issues detected. Continue? (y/N): ").strip().lower()
            if ok != "y":
                log("Cancelled by user", "err")
                ctx.failed_step = "scoring"
                return False
        except (EOFError, KeyboardInterrupt):
            return False
    return True


STEPS = {
    "validate": step_validate,
    "scoring": step_scoring,
    "pregate": step_pregate,
    "branch": step_branch,
    "commit": step_commit,
    "push": step_push,
    "create_pr": step_create_pr,
    "add_reviewers": step_add_reviewers,
    "evidence": step_evidence,
}

def run_pipeline(ctx: StepContext, steps: list[str]) -> bool:
    """Run pipeline steps in order."""
    log(f"Pipeline: {' → '.join(steps)}", "info")

    if ctx.dry_run:
        log("DRY RUN — no changes will be made\n", "dry")

    for step_name in steps:
        if step_name not in STEPS:
            log(f"Unknown step: {step_name}", "err")
            continue

        log(f"\n  [{step_name}]", "info")
        fn = STEPS[step_name]
        ok = fn(ctx)
        if ctx.dry_run:
            continue
        if not ok:
            log(f"Pipeline failed at step: {step_name}", "err")
            ctx.failed_step = step_name
            save_state(ctx.to_dict())
            return False

    if not ctx.dry_run:
        clear_state()
        log("\nPipeline complete!", "ok")
    else:
        log("\nDry-run complete — no changes made", "ok")

    return True


# ── THRESHOLD CHECK ────────────────────────────────────────────────────────────

def check_threshold(cfg: dict) -> dict:
    """Check open PR threshold: min(10 + floor(token_score/300), 30)."""
    repo = cfg["repo"]
    r = run(["gh", "pr", "list", "--repo", repo, "--state", "open",
             "--author", "alpurkan17", "--json", "number", "-L", "30"], timeout=15)
    open_prs = json.loads(r.stdout) if r and r.stdout.strip() else []
    count = len(open_prs)

    # Fetch real token_score from Gittensor API
    token_score = 0
    try:
        api_r = subprocess.run(
            ["curl", "-s", "https://api.gittensor.io/miners/88453512"],
            capture_output=True, text=True, timeout=10
        )
        if api_r.returncode == 0 and api_r.stdout.strip():
            api_data = json.loads(api_r.stdout)
            token_score = int(float(api_data.get("totalTokenScore", "0")))
    except Exception:
        pass

    max_prs = min(10 + token_score // 300, 30)

    return {
        "open": count,
        "max": max_prs,
        "available": max_prs - count,
        "prs": [p["number"] for p in open_prs],
        "repo": repo,
        "token_score": token_score,
    }


# ── BATCH MODE ─────────────────────────────────────────────────────────────────

def run_batch(config: dict, batch_file: str, dry_run: bool = False):
    """Create multiple PRs from a batch config file."""
    batch = load_json(batch_file)
    if not batch:
        log(f"Failed to load batch file: {batch_file}", "err")
        sys.exit(1)

    prs = batch if isinstance(batch, list) else batch.get("prs", [])
    interval = batch.get("interval_minutes", 0) if isinstance(batch, dict) else 0

    log(f"Batch: {len(prs)} PR(s), {interval}min interval\n", "info")

    results = []
    for i, entry in enumerate(prs, 1):
        issue = entry.get("issue")
        prefix = entry.get("prefix", "fix")
        desc = entry.get("desc", entry.get("title", ""))
        slug = make_slug(desc)
        title = f"{prefix}: {desc} (#{issue})" if issue else f"{prefix}: {desc}"

        ctx = StepContext(
            config=config,
            issue=issue,
            prefix=prefix,
            title=title,
            desc=desc,
            slug=slug,
            dry_run=dry_run,
            **{k: entry.get(k) for k in ["root_cause", "impact", "why", "tests",
                                          "live_verif", "how_to_verify", "edge_cases",
                                          "post_merge", "cross_refs", "out_of_scope"]
               if entry.get(k)},
        )
        # Load extra fields
        for k in ["root_cause", "impact", "why", "tests", "live_verif",
                   "how_to_verify", "edge_cases", "post_merge", "cross_refs", "out_of_scope"]:
            if entry.get(k):
                ctx._extra[k] = entry[k]

        # Auto-detect files from diff (only for first PR if same branch context)
        auto_detect(ctx)
        ctx.body = build_body(ctx)

        steps = config.get("steps", DEFAULT_CONFIG["steps"])
        ok = run_pipeline(ctx, steps)

        results.append({"issue": issue, "title": title, "ok": ok, "pr": ctx.pr_number})

        if i < len(prs) and interval > 0 and not dry_run:
            log(f"Waiting {interval}min before next PR...", "info")
            time.sleep(interval * 60)

    # Summary
    ok_count = sum(1 for r in results if r["ok"])
    log(f"\nBatch complete: {ok_count}/{len(results)} succeeded", "ok" if ok_count == len(results) else "warn")
    for r in results:
        if dry_run:
            status = "DRY RUN"
        elif r["pr"]:
            status = f"PR #{r['pr']}"
        else:
            status = "FAILED"
        log(f"  #{r['issue']}: {status}", "dry" if dry_run else ("ok" if r["pr"] else "err"))

    return results


# ── MAIN ───────────────────────────────────────────────────────────────────────

def list_presets():
    """Display available body style presets."""
    print(f"\n  Available body style presets:\n")
    for name, preset in PRESETS.items():
        sections = ", ".join(preset["sections"])
        print(f"  {name:<15} {sections}")
    print()


def main():
    p = argparse.ArgumentParser(
        description="PR Flow — Flexible PR workflow orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s --issue 123 --prefix fix --desc "handle null fields"
              %(prog)s --config pr_config.yaml
              %(prog)s --batch pr_batch.json
              %(prog)s --issue 123 --dry-run
              %(prog)s --resume
              %(prog)s --list-presets
              %(prog)s --issue 123 --steps validate,commit,push --no-pr
        """),
    )
    p.add_argument("--issue", type=int, help="Issue number")
    p.add_argument("--prefix", default="fix", help="PR prefix (fix/feat/perf/...), default: fix")
    p.add_argument("--title", help="PR title (default: auto from prefix+desc+issue)")
    p.add_argument("--desc", help="Short description for branch/commit")
    p.add_argument("--config", help="Path to YAML/JSON config file")
    p.add_argument("--batch", help="Batch JSON file with multiple PR definitions")
    p.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    p.add_argument("--resume", action="store_true", help="Resume failed pipeline from state file")
    p.add_argument("--steps", help="Comma-separated steps to run (default: all)")
    p.add_argument("--no-pr", action="store_true", help="Skip PR creation (push only)")
    p.add_argument("--loose", action="store_true", help="Skip strict validation")
    p.add_argument("--list-presets", action="store_true", help="Show available body style presets")
    p.add_argument("--status", action="store_true", help="Check open PR threshold")
    p.add_argument("--root-cause", help="Root cause analysis")
    p.add_argument("--impact", help="Impact description")
    p.add_argument("--why", help="Broader context")
    p.add_argument("--tests", nargs="+", help="Test plan items")
    p.add_argument("--live-verif", nargs="*", help="Live verification commands")
    p.add_argument("--how-to-verify", nargs="*", help="Verification steps for reviewer")
    p.add_argument("--edge-cases", nargs="*", help="Edge cases considered")
    p.add_argument("--post-merge", nargs="*", help="Post-merge verification items")
    p.add_argument("--cross-refs", nargs="*", help="Cross-references")
    p.add_argument("--out-of-scope", help="What is NOT covered")
    p.add_argument("--caption", help="Evidence caption for terminal capture")
    p.add_argument("--body-file", help="Load PR body from file (skip generation)")
    p.add_argument("--body-only", action="store_true", help="Generate body only, no pipeline")
    args = p.parse_args()

    # ── Special modes ──
    if args.list_presets:
        list_presets()
        return

    config = load_config(args.config)

    if args.no_pr:
        # Remove create_pr and add_reviewers from steps
        config["steps"] = [s for s in config["steps"] if s not in ("create_pr", "add_reviewers")]

    if args.loose:
        config["strict_validation"] = False

    if args.status:
        t = check_threshold(config)
        log(f"Repo: {t['repo']}", "info")
        log(f"Open PRs: {t['open']}/{t['max']}", "info")
        log(f"Available slots: {t['available']}", "info" if t['available'] > 0 else "warn")
        return

    # ── Resume mode ──
    if args.resume:
        state = load_state()
        if not state:
            log("No saved state found to resume", "err")
            sys.exit(1)
        log(f"Resuming from failed step: {state.get('failed_step', 'unknown')}", "info")
        ctx = StepContext.from_dict(state, config)
        ctx.dry_run = args.dry_run or False

        steps = config.get("steps", DEFAULT_CONFIG["steps"])
        failed_idx = next((i for i, s in enumerate(steps) if s == ctx.failed_step), 0)
        remaining = steps[failed_idx:]

        ok = run_pipeline(ctx, remaining)
        if ok:
            log("Resume complete!", "ok")
        else:
            log("Resume failed, check state file", "err")
        return

    # ── Batch mode ──
    if args.batch:
        run_batch(config, args.batch, args.dry_run)
        return

    # ── Single PR mode ──
    if not args.issue:
        p.print_help()
        sys.exit(1)

    # Check threshold (skip check for --body-only or --dry-run)
    if not args.body_only and not args.dry_run:
        t = check_threshold(config)
        if t["available"] <= 0:
            log(f"Threshold full ({t['open']}/{t['max']}). Cannot create PR.", "err")
            log("Use --dry-run to preview, or --body-only to generate body only", "info")
            sys.exit(1)
        else:
            log(f"Threshold: {t['open']}/{t['max']} ({t['available']} slots available)", "ok")

    # Determine prefix is valid
    prefix = args.prefix
    valid_prefixes = config.get("valid_prefixes", DEFAULT_CONFIG["valid_prefixes"])
    if prefix not in valid_prefixes:
        log(f"Invalid prefix '{prefix}'. Valid: {', '.join(valid_prefixes)}", "err")
        sys.exit(1)

    # Auto-detect description from issue title
    desc = args.desc
    title = args.title

    if not desc or not title:
        r = run(["gh", "issue", "view", str(args.issue), "--repo", config["repo"],
                 "--json", "title", "-q", ".title"], timeout=15, check=False)
        issue_title = r.stdout.strip() if r and r.stdout.strip() else ""
        if not desc:
            desc = issue_title[:80] if issue_title else f"fix issue #{args.issue}"
        if not title:
            title = f"{prefix}: {desc} (#{args.issue})"

    slug = make_slug(desc)

    # Build context
    ctx = StepContext(
        config=config,
        issue=args.issue,
        prefix=prefix,
        title=title or f"{prefix}: {desc} (#{args.issue})",
        desc=desc,
        slug=slug,
        dry_run=args.dry_run or False,
    )

    # Load extra fields
    for k in ["root_cause", "impact", "why", "tests", "live_verif",
               "how_to_verify", "edge_cases", "post_merge", "cross_refs", "out_of_scope"]:
        v = getattr(args, k, None)
        if v:
            ctx._extra[k] = v

    # Auto-detect files
    auto_detect(ctx)

    # Load body from file or generate
    if args.body_file:
        with open(args.body_file) as f:
            ctx.body = f.read()
        log(f"Body loaded from {args.body_file}", "info")
        # Validate body sections (both old v3 and new format accepted)
        has_summary = "## Summary" in ctx.body
        has_old_v3 = all(s in ctx.body for s in ["## Root Cause", "## Impact", "## Test plan"])
        has_new = "## Validation" in ctx.body or "Closes #" in ctx.body
        if not has_summary:
            log("BODY MISSING ## Summary section", "warn")
        elif not has_old_v3 and not has_new:
            log("BODY: neither old v3 nor new format detected (missing Validation/Test plan)", "warn")
        elif has_new:
            log("Body: simplified format (Summary + Validation + Closes)", "info")
        else:
            log("Body: v3 format (Summary + Root Cause + Impact + Test plan)", "info")
    else:
        ctx.body = build_body(ctx)

    if args.body_only:
        print(f"\n{ctx.body}\n")
        return

    # Determine steps
    if args.steps:
        steps = [s.strip() for s in args.steps.split(",")]
    else:
        steps = config.get("steps", DEFAULT_CONFIG["steps"])

    # Run pipeline
    ok = run_pipeline(ctx, steps)

    if ok and ctx.pr_number:
        print(f"\n  {'='*50}")
        print(f"  PR #{ctx.pr_number}: {ctx.pr_url}")
        print(f"  {'='*50}")
        print(f"\n  Next: python3 scripts/capture_terminal.py --pr {ctx.pr_number}")
        print(f"        python3 scripts/pr_dashboard.py")
    elif ok and not ctx.pr_number and not args.no_pr:
        print(f"\n  Pipeline steps completed (no PR created)")
    elif not ok:
        print(f"\n  Pipeline failed at: {ctx.failed_step}")
        print(f"  Fix the issue and resume: python3 scripts/pr_flow.py --resume")
        sys.exit(1)


if __name__ == "__main__":
    main()
