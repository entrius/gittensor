# Entrius 2025

"""gitt miner scan — Discover high-value open issues across incentivised repositories.

For every repo in master_repositories.json the command fetches open GitHub
issues, scores each one by expected TAO opportunity, and surfaces a ranked
action list so the miner can focus effort where the reward is highest.

Opportunity score formula
─────────────────────────
  opportunity = emission_share
              × potential_issue_multiplier   (1.66 if maintainer-authored, else 1.33)
              × label_bonus                  (best matching label multiplier, or 1.0)
              × competition_factor           (1.0 no competing PR, 0.4 has one)
              × freshness_factor             (1.0 if ≤14d old, decays to 0.3 at 60d)

Higher = more TAO for the same PR effort.
"""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import click
import requests
from rich.console import Console
from rich.table import Table
from rich.text import Text

from gittensor.cli.miner_commands.helpers import (
    PIPELINE_DEV_HOTKEY,
    PIPELINE_DEV_UID,
    LocalValidatorStub,
    override_pats_file,
    _error,
)
from gittensor.constants import (
    BASE_GITHUB_API_URL,
    GITHUB_HTTP_TIMEOUT_SECONDS,
    MAINTAINER_ASSOCIATIONS,
)
from gittensor.utils.github_api_tools import make_headers

console = Console()
err = Console(stderr=True)

# Maximum issues fetched per repo (1 API page = 30 items)
_ISSUES_PER_REPO = 30
# Repos with emission_share below this are skipped to save API calls
_MIN_EMISSION_SHARE = 0.005
# How many top opportunities to display
_TOP_N = 30


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Opportunity:
    repo: str
    issue_number: int
    title: str
    url: str
    author_association: str
    labels: List[str]
    created_at: datetime
    comments: int
    has_competing_pr: bool
    emission_share: float
    issue_discovery_share: float
    opportunity_score: float
    potential_multiplier: float
    label_bonus: float
    competition_factor: float
    freshness_factor: float
    tip: str = ""


# ---------------------------------------------------------------------------
# GitHub REST helpers
# ---------------------------------------------------------------------------


def _fetch_open_issues(
    session: requests.Session,
    repo: str,
    per_page: int = _ISSUES_PER_REPO,
) -> List[Dict]:
    """Fetch the most recently updated open issues for a repo (1 page)."""
    url = f"{BASE_GITHUB_API_URL}/repos/{repo}/issues"
    try:
        resp = session.get(
            url,
            params={"state": "open", "per_page": per_page, "sort": "updated", "direction": "desc"},
            timeout=GITHUB_HTTP_TIMEOUT_SECONDS,
        )
        if resp.status_code == 404:
            return []
        if resp.status_code == 403:
            remaining = resp.headers.get("x-ratelimit-remaining", "?")
            err.print(f"[yellow]Rate-limited or no access for {repo} (remaining={remaining})[/yellow]")
            if remaining == "0":
                reset = int(resp.headers.get("x-ratelimit-reset", time.time() + 60))
                wait = max(1, reset - int(time.time()))
                err.print(f"[yellow]Rate limit reset in {wait}s. Waiting…[/yellow]")
                time.sleep(min(wait, 60))
            return []
        resp.raise_for_status()
        # GitHub returns PRs in the issues endpoint; filter them out
        return [i for i in resp.json() if "pull_request" not in i]
    except requests.RequestException as exc:
        err.print(f"[dim]Could not fetch issues for {repo}: {exc}[/dim]")
        return []


def _check_competing_prs(session: requests.Session, repo: str, issue_number: int) -> bool:
    """Return True if any open PR in the repo references this issue number."""
    search_query = f"repo:{repo} is:pr is:open #{issue_number}"
    try:
        resp = session.get(
            f"{BASE_GITHUB_API_URL}/search/issues",
            params={"q": search_query, "per_page": 1},
            timeout=GITHUB_HTTP_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            return False
        return resp.json().get("total_count", 0) > 0
    except requests.RequestException:
        return False


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def _potential_issue_multiplier(author_association: str, repo_cfg) -> float:
    """Return the likely issue multiplier if this issue is solved."""
    from gittensor.validator.utils.load_weights import resolve_scoring

    scoring = resolve_scoring(repo_cfg.scoring)
    if author_association in MAINTAINER_ASSOCIATIONS:
        return scoring.maintainer_issue_multiplier
    return scoring.standard_issue_multiplier


def _best_label_bonus(issue_labels: List[str], repo_cfg) -> Tuple[float, str]:
    """Return (best_multiplier, label_name) from the repo's label_multipliers config."""
    if not repo_cfg.label_multipliers or not issue_labels:
        return 1.0, ""
    best = 1.0
    best_name = ""
    for label in issue_labels:
        lval = repo_cfg.label_multipliers.get(label)
        if lval is not None and lval > best:
            best = lval
            best_name = label
    return best, best_name


def _freshness_factor(created_at: datetime) -> float:
    """Issues older than 60 days decay; very fresh issues are prioritised."""
    days = (datetime.now(timezone.utc) - created_at).total_seconds() / 86400
    if days <= 14:
        return 1.0
    # Sigmoid decay: 0.5 at 30 days, 0.3 floor at 60+ days
    raw = 1.0 / (1.0 + math.exp(0.15 * (days - 30)))
    return max(0.3, raw)


def _parse_dt(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _score_issue(
    repo: str,
    raw: Dict,
    repo_cfg,
    check_prs: bool,
    session: requests.Session,
) -> Optional[Opportunity]:
    """Compute an Opportunity for a single GitHub issue dict."""
    issue_number = raw.get("number")
    title = raw.get("title", "")
    url = raw.get("html_url", f"https://github.com/{repo}/issues/{issue_number}")
    author_assoc = (raw.get("author_association") or "NONE").upper()
    labels = [lbl["name"] for lbl in (raw.get("labels") or [])]
    comments = raw.get("comments", 0)
    created_at = _parse_dt(raw.get("created_at"))
    if created_at is None:
        return None

    # default_label_multiplier=0.0 means only labelled issues score → skip unlabelled
    if repo_cfg.default_label_multiplier == 0.0 and not labels:
        return None

    has_competing = False
    if check_prs:
        has_competing = _check_competing_prs(session, repo, issue_number)

    pot_mult = _potential_issue_multiplier(author_assoc, repo_cfg)
    label_bonus, label_name = _best_label_bonus(labels, repo_cfg)
    comp_factor = 0.4 if has_competing else 1.0
    fresh = _freshness_factor(created_at)

    score = repo_cfg.emission_share * pot_mult * label_bonus * comp_factor * fresh

    # Build human-readable tip
    tips = []
    if author_assoc in MAINTAINER_ASSOCIATIONS:
        tips.append(f"maintainer issue → {pot_mult:.2f}x")
    if label_name:
        tips.append(f"label '{label_name}' → {label_bonus:.2f}x")
    if has_competing:
        tips.append("competing PR exists")
    tip = " | ".join(tips) if tips else ""

    return Opportunity(
        repo=repo,
        issue_number=issue_number,
        title=title[:72],
        url=url,
        author_association=author_assoc,
        labels=labels,
        created_at=created_at,
        comments=comments,
        has_competing_pr=has_competing,
        emission_share=repo_cfg.emission_share,
        issue_discovery_share=repo_cfg.issue_discovery_share,
        opportunity_score=score,
        potential_multiplier=pot_mult,
        label_bonus=label_bonus,
        competition_factor=comp_factor,
        freshness_factor=fresh,
        tip=tip,
    )


# ---------------------------------------------------------------------------
# Eligibility gap helper
# ---------------------------------------------------------------------------


@dataclass
class EligibilityGap:
    repo: str
    emission_share: float
    have_merged: int
    need_merged: int
    credibility: float
    min_credibility: float


def _eligibility_gaps(miner_eval, master_repositories: Dict) -> List[EligibilityGap]:
    """Return repos where the miner is not yet eligible but close (within 3 PRs)."""
    from gittensor.validator.utils.load_weights import resolve_eligibility

    gaps: List[EligibilityGap] = []
    for repo_name, cfg in master_repositories.items():
        if cfg.emission_share <= 0:
            continue
        elig = resolve_eligibility(cfg.eligibility)
        merged = [pr for pr in miner_eval.merged_prs if pr.pr.repo_full_name.lower() == repo_name]
        closed = [pr for pr in miner_eval.closed_prs if pr.pr.repo_full_name.lower() == repo_name]
        n_merged = len(merged)
        n_closed = len(closed)
        total = n_merged + n_closed
        cred = n_merged / total if total > 0 else 0.0

        missing_prs = elig.min_valid_merged_prs - n_merged
        cred_ok = cred >= elig.min_credibility or total == 0

        if missing_prs > 0 and missing_prs <= 3:
            gaps.append(
                EligibilityGap(
                    repo=repo_name,
                    emission_share=cfg.emission_share,
                    have_merged=n_merged,
                    need_merged=elig.min_valid_merged_prs,
                    credibility=cred,
                    min_credibility=elig.min_credibility,
                )
            )
    return sorted(gaps, key=lambda g: -g.emission_share)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_opportunities(opps: List[Opportunity], top_n: int) -> None:
    shown = opps[:top_n]
    if not shown:
        console.print("[yellow]No open issues found in incentivised repos.[/yellow]")
        return

    table = Table(
        title=f"Top {len(shown)} Issue Opportunities",
        show_lines=True,
    )
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Score", justify="right", style="bold green", no_wrap=True)
    table.add_column("Repo", style="cyan", no_wrap=True)
    table.add_column("Issue", no_wrap=False)
    table.add_column("Labels", style="magenta", no_wrap=True)
    table.add_column("Mult", justify="right", no_wrap=True)
    table.add_column("Compete", justify="center", no_wrap=True)
    table.add_column("Age(d)", justify="right", no_wrap=True)
    table.add_column("Tip", style="dim", no_wrap=False)

    for rank, opp in enumerate(shown, start=1):
        age_days = (datetime.now(timezone.utc) - opp.created_at).total_seconds() / 86400
        compete_str = (
            Text("✗ yes", style="red") if opp.has_competing_pr else Text("✓ free", style="green")
        )
        issue_cell = f"#{opp.issue_number} {opp.title}"
        table.add_row(
            str(rank),
            f"{opp.opportunity_score:.4f}",
            opp.repo,
            issue_cell,
            ", ".join(opp.labels) or "-",
            f"{opp.potential_multiplier:.2f}x",
            compete_str,
            f"{age_days:.0f}",
            opp.tip or "-",
        )

    console.print(table)
    console.print(
        "\n[dim]Score = emission_share × issue_multiplier × label_bonus × competition × freshness[/dim]"
    )


def _render_gaps(gaps: List[EligibilityGap]) -> None:
    if not gaps:
        return
    table = Table(title="Almost Eligible (≤3 PRs away)", show_lines=False)
    table.add_column("Repo", style="cyan")
    table.add_column("Emission", justify="right")
    table.add_column("Merged", justify="right")
    table.add_column("Need", justify="right")
    table.add_column("Credibility", justify="right")

    for g in gaps:
        cred_style = "green" if g.credibility >= g.min_credibility else "red"
        table.add_row(
            g.repo,
            f"{g.emission_share:.1%}",
            str(g.have_merged),
            str(g.need_merged),
            f"[{cred_style}]{g.credibility:.0%}[/{cred_style}]",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command(name="scan")
@click.option(
    "--pat",
    default=None,
    envvar="GITTENSOR_MINER_PAT",
    help="GitHub Personal Access Token. Uses GITTENSOR_MINER_PAT env if unset.",
)
@click.option(
    "--top",
    type=int,
    default=_TOP_N,
    show_default=True,
    help="Number of top opportunities to display.",
)
@click.option(
    "--min-emission",
    type=float,
    default=_MIN_EMISSION_SHARE,
    show_default=True,
    help="Skip repos with emission_share below this value.",
)
@click.option(
    "--check-prs",
    is_flag=True,
    default=False,
    help="Check each issue for competing open PRs (slower — uses extra API calls).",
)
@click.option(
    "--gaps",
    is_flag=True,
    default=False,
    help="Also show repos where you are ≤3 PRs from becoming eligible (requires --score-pat).",
)
def scan_command(
    pat: Optional[str],
    top: int,
    min_emission: float,
    check_prs: bool,
    gaps: bool,
) -> None:
    """Discover high-value open issues across incentivised repositories.

    Scans every repo in master_repositories.json and ranks open GitHub issues
    by opportunity score so you know exactly where to focus your next PR.

    \\b
    Opportunity score:
        emission_share × issue_multiplier × label_bonus × competition × freshness

    \\b
    Examples:
        gitt miner scan --pat ghp_xxxxx
        gitt miner scan --pat ghp_xxxxx --check-prs --top 20
        GITTENSOR_MINER_PAT=ghp_xxxxx gitt miner scan --min-emission 0.03
    """
    if not pat:
        _error("--pat flag or GITTENSOR_MINER_PAT environment variable is required.", False)
        sys.exit(1)

    from gittensor.validator.utils.load_weights import load_master_repo_weights

    with console.status("[bold cyan]Loading repository registry…", spinner="dots"):
        master_repositories = load_master_repo_weights()

    target_repos = {
        name: cfg
        for name, cfg in sorted(master_repositories.items(), key=lambda x: -x[1].emission_share)
        if cfg.emission_share >= min_emission
    }

    err.print(
        f"[dim]Scanning {len(target_repos)} repos "
        f"(emission_share ≥ {min_emission:.1%}) …[/dim]"
    )

    session = requests.Session()
    session.headers.update(make_headers(pat))

    opportunities: List[Opportunity] = []

    with console.status("[bold cyan]Fetching open issues…", spinner="dots") as status:
        for repo, cfg in target_repos.items():
            status.update(f"[bold cyan]Fetching issues: {repo}[/bold cyan]")
            raw_issues = _fetch_open_issues(session, repo)
            for raw in raw_issues:
                opp = _score_issue(repo, raw, cfg, check_prs, session)
                if opp is not None:
                    opportunities.append(opp)

    opportunities.sort(key=lambda o: -o.opportunity_score)
    console.print()
    _render_opportunities(opportunities, top)

    if gaps:
        import asyncio
        from typing import Any, cast

        import bittensor as bt

        from gittensor.validator.forward import oss_contributions
        from gittensor.validator.utils.load_weights import (
            load_programming_language_weights,
            load_token_config,
        )

        bt.logging.set_warning()

        with console.status("[bold cyan]Running scoring pipeline for eligibility gaps…", spinner="dots"):
            langs = load_programming_language_weights()
            token_cfg = load_token_config()
            stub = LocalValidatorStub()
            snapshot = [{"uid": PIPELINE_DEV_UID, "hotkey": PIPELINE_DEV_HOTKEY, "pat": pat}]

            async def _run():
                with override_pats_file(snapshot):
                    evals, _, _ = await oss_contributions(
                        cast(Any, stub), {PIPELINE_DEV_UID}, master_repositories, langs, token_cfg
                    )
                return evals[PIPELINE_DEV_UID]

            miner_eval = asyncio.run(_run())

        gap_list = _eligibility_gaps(miner_eval, target_repos)
        console.print()
        _render_gaps(gap_list)
