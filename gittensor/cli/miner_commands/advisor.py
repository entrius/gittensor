# Entrius 2025

"""gitt miner advisor — Analyse scoring results and surface actionable recommendations.

Runs the same validator pipeline as ``gitt miner score`` but, instead of
dumping raw numbers, interprets them and tells the miner exactly what to do
next to maximise their reward.

Categories of advice produced:
- CRITICAL  Eligibility blockers — fix these or earn nothing in a repo
- WARNING   Active penalties cutting your score right now
- TIP       Multiplier opportunities you are not yet using
- INFO      Context useful for planning (time decay, label opportunities, etc.)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, cast

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from gittensor.cli.miner_commands.helpers import (
    PIPELINE_DEV_HOTKEY,
    PIPELINE_DEV_UID,
    LocalValidatorStub,
    drain_logs,
    override_pats_file,
    _error,
)
from gittensor.validator.oss_contributions.scoring import calculate_open_pr_threshold
from gittensor.validator.utils.load_weights import (
    RepositoryConfig,
    resolve_eligibility,
    resolve_scoring,
)

console = Console()


# ---------------------------------------------------------------------------
# Advice model
# ---------------------------------------------------------------------------


class Severity(Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    TIP = "TIP"
    INFO = "INFO"


_SEVERITY_STYLE = {
    Severity.CRITICAL: "bold red",
    Severity.WARNING: "yellow",
    Severity.TIP: "bold cyan",
    Severity.INFO: "dim",
}

_SEVERITY_ICON = {
    Severity.CRITICAL: "✗",
    Severity.WARNING: "⚠",
    Severity.TIP: "★",
    Severity.INFO: "·",
}


@dataclass
class Advice:
    severity: Severity
    repo: Optional[str]
    message: str


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------


def _days_since(dt: datetime) -> float:
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def _analyse(
    miner_eval: Any,
    master_repositories: Dict[str, RepositoryConfig],
    rewards_payload: Dict[str, Any],
) -> List[Advice]:
    """Derive a list of Advice objects from a completed MinerEvaluation."""
    advice: List[Advice] = []

    if miner_eval.failed_reason:
        advice.append(
            Advice(
                Severity.CRITICAL,
                None,
                f"Identity validation failed: {miner_eval.failed_reason}. "
                "Register a valid GitHub PAT with: gitt miner post",
            )
        )
        return advice

    for repo_name, repo_config in sorted(master_repositories.items(), key=lambda x: -x[1].emission_share):
        if repo_config.emission_share <= 0:
            continue

        repo_eval = miner_eval.repo_evaluations.get(repo_name)
        merged = [pr for pr in miner_eval.merged_prs if pr.pr.repo_full_name.lower() == repo_name]
        closed = [pr for pr in miner_eval.closed_prs if pr.pr.repo_full_name.lower() == repo_name]
        open_prs = [pr for pr in miner_eval.open_prs if pr.pr.repo_full_name.lower() == repo_name]

        elig_cfg = resolve_eligibility(repo_config.eligibility)
        scoring_cfg = resolve_scoring(repo_config.scoring)

        merged_count = len(merged)
        closed_count = len(closed)
        open_count = len(open_prs)
        total_attempts = merged_count + closed_count
        credibility = merged_count / total_attempts if total_attempts > 0 else 0.0

        # --- eligibility gate ---
        if merged_count < elig_cfg.min_valid_merged_prs:
            need = elig_cfg.min_valid_merged_prs - merged_count
            advice.append(
                Advice(
                    Severity.CRITICAL,
                    repo_name,
                    f"Need {need} more merged PR(s) to unlock scoring "
                    f"(have {merged_count}/{elig_cfg.min_valid_merged_prs}). "
                    f"emission_share={repo_config.emission_share:.1%}",
                )
            )
        elif credibility < elig_cfg.min_credibility:
            advice.append(
                Advice(
                    Severity.CRITICAL,
                    repo_name,
                    f"Credibility {credibility:.0%} below minimum {elig_cfg.min_credibility:.0%} "
                    f"(merged={merged_count}, closed={closed_count}). "
                    "Avoid submitting PRs that are likely to be closed without merging.",
                )
            )

        # --- open-PR spam penalty ---
        total_token_score = sum(pr.token_score for pr in merged)
        spam_threshold = calculate_open_pr_threshold(elig_cfg, total_token_score)
        if open_count > spam_threshold:
            advice.append(
                Advice(
                    Severity.WARNING,
                    repo_name,
                    f"Open PR spam penalty active: {open_count} open PRs > threshold {spam_threshold}. "
                    "ALL earned scores for this repo are zeroed. "
                    f"Close {open_count - spam_threshold} open PR(s) to restore scoring.",
                )
            )
        elif open_count == spam_threshold and spam_threshold > 0:
            advice.append(
                Advice(
                    Severity.WARNING,
                    repo_name,
                    f"At the open-PR limit ({open_count}/{spam_threshold}). "
                    "One more open PR will zero all earned scores for this repo.",
                )
            )

        # --- time decay on merged PRs ---
        for spr in merged:
            if spr.pr.merged_at is None:
                continue
            days = _days_since(spr.pr.merged_at)
            midpoint = scoring_cfg.time_decay.sigmoid_midpoint_days
            if days >= midpoint:
                decay = spr.time_decay_multiplier
                advice.append(
                    Advice(
                        Severity.WARNING,
                        repo_name,
                        f"PR #{spr.pr.pr_number} is {days:.0f}d old "
                        f"(time decay={decay:.2f}x, midpoint={midpoint:.0f}d). "
                        "Score will continue to drop — open a new PR to replace it.",
                    )
                )
            elif days >= scoring_cfg.time_decay.grace_period_hours / 24 * 3:
                decay = spr.time_decay_multiplier
                if decay < 0.9:
                    advice.append(
                        Advice(
                            Severity.INFO,
                            repo_name,
                            f"PR #{spr.pr.pr_number} is {days:.0f}d old (time decay={decay:.2f}x). "
                            "Consider submitting new PRs before this decays further.",
                        )
                    )

        # --- review quality penalty ---
        for spr in merged:
            if spr.review_quality_multiplier < 1.0:
                penalty_pct = (1.0 - spr.review_quality_multiplier) * 100
                advice.append(
                    Advice(
                        Severity.WARNING,
                        repo_name,
                        f"PR #{spr.pr.pr_number} has review quality penalty "
                        f"({penalty_pct:.0f}% deduction from CHANGES_REQUESTED reviews). "
                        "Address maintainer feedback thoroughly before submitting.",
                    )
                )

        # --- issue multiplier opportunity ---
        if repo_eval and repo_eval.is_eligible:
            prs_without_issue = [spr for spr in merged if spr.issue_multiplier <= 1.0]
            if prs_without_issue:
                advice.append(
                    Advice(
                        Severity.TIP,
                        repo_name,
                        f"{len(prs_without_issue)} merged PR(s) lack an issue link "
                        f"(standard multiplier=1.33x, maintainer issue=1.66x). "
                        "Find open issues in this repo and link your next PR to one.",
                    )
                )

        # --- label multiplier opportunity ---
        if repo_config.label_multipliers and repo_eval and repo_eval.is_eligible:
            best_label = max(repo_config.label_multipliers.items(), key=lambda x: x[1])
            if best_label[1] > 1.0:
                prs_without_label = [
                    spr for spr in merged if spr.label is None or spr.label_multiplier <= 1.0
                ]
                if prs_without_label:
                    advice.append(
                        Advice(
                            Severity.TIP,
                            repo_name,
                            f"Best scoring label for this repo: '{best_label[0]}' ({best_label[1]:.2f}x). "
                            f"{len(prs_without_label)} merged PR(s) are not benefiting from a label. "
                            "Ask maintainers to apply scoring labels to eligible PRs.",
                        )
                    )

        # --- issue discovery opportunity ---
        if repo_config.issue_discovery_share > 0 and repo_eval:
            if repo_eval.issue_discovery_score <= 0 and repo_eval.is_eligible:
                advice.append(
                    Advice(
                        Severity.TIP,
                        repo_name,
                        f"Issue discovery earns {repo_config.issue_discovery_share:.0%} of this repo's "
                        "emission slice but your score is 0. "
                        "Open high-quality issues in this repo to earn discovery rewards.",
                    )
                )

    # ------------------------------------------------------------------ #
    # Global / cross-repo observations
    # ------------------------------------------------------------------ #

    untouched_high_value = [
        (name, cfg)
        for name, cfg in sorted(master_repositories.items(), key=lambda x: -x[1].emission_share)
        if cfg.emission_share >= 0.04
        and name not in miner_eval.unique_repos_contributed_to
        and cfg.emission_share > 0
    ]
    if untouched_high_value:
        repos_str = ", ".join(
            f"{name} ({cfg.emission_share:.1%})" for name, cfg in untouched_high_value[:3]
        )
        advice.append(
            Advice(
                Severity.TIP,
                None,
                f"High-value repos with no contributions yet: {repos_str}. "
                "Diversifying across multiple repos increases total reward.",
            )
        )

    if miner_eval.issue_discovery_score <= 0:
        discovery_repos = [
            name
            for name, cfg in master_repositories.items()
            if cfg.issue_discovery_share > 0 and cfg.emission_share > 0
        ]
        if discovery_repos:
            advice.append(
                Advice(
                    Severity.TIP,
                    None,
                    f"Issue discovery score is 0 across all repos. "
                    f"{len(discovery_repos)} repo(s) allocate emission to issue discovery. "
                    "Opening issues that get solved by other contributors earns extra rewards.",
                )
            )

    return advice


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_advice(advice_list: List[Advice], miner_eval: Any, reward: float) -> None:
    console.print()
    console.print(
        Panel(
            f"[bold]GitHub ID:[/bold] {miner_eval.github_id}  "
            f"[bold]Blended reward:[/bold] [bold green]{reward:.6f}[/bold green]  "
            f"[bold]Eligible repos:[/bold] "
            + str(sum(1 for re in miner_eval.repo_evaluations.values() if re.is_eligible)),
            title="Miner Advisor",
            expand=False,
        )
    )

    if not advice_list:
        console.print("[bold green]No issues found. Your setup looks optimal.[/bold green]")
        return

    order = [Severity.CRITICAL, Severity.WARNING, Severity.TIP, Severity.INFO]
    for sev in order:
        items = [a for a in advice_list if a.severity == sev]
        if not items:
            continue

        table = Table(
            title=f"{_SEVERITY_ICON[sev]}  {sev.value}",
            title_style=_SEVERITY_STYLE[sev],
            show_header=True,
            show_lines=True,
        )
        table.add_column("Repo", style="cyan", no_wrap=True, min_width=28)
        table.add_column("Recommendation", overflow="fold")

        for a in items:
            table.add_row(a.repo or "(global)", a.message)

        console.print(table)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command(name="advisor")
@click.option(
    "--pat",
    default=None,
    envvar="GITTENSOR_MINER_PAT",
    help="GitHub Personal Access Token. Uses GITTENSOR_MINER_PAT env if unset.",
)
@click.option(
    "--log-level",
    type=click.Choice(["warning", "info", "debug", "trace"]),
    default="warning",
    show_default=True,
    help="Bittensor log verbosity.",
)
def advisor_command(pat: Optional[str], log_level: str) -> None:
    """Analyse your scoring results and surface actionable recommendations.

    Runs the full validator scoring pipeline locally (no subtensor, wallet, or
    DB required), then interprets the output to tell you exactly what to do
    next to maximise your reward.

    \\b
    Advice categories:
        CRITICAL  Eligibility blockers — fix these or earn nothing
        WARNING   Active penalties cutting your score right now
        TIP       Multiplier opportunities you are not yet using
        INFO      Context useful for planning

    \\b
    Examples:
        gitt miner advisor --pat ghp_xxxxx
        GITTENSOR_MINER_PAT=ghp_xxxxx gitt miner advisor
    """
    import asyncio
    import bittensor as bt

    from gittensor.validator.emission_allocation import blend_emission_pools
    from gittensor.validator.forward import build_maintainer_uids_by_repo, issue_discovery, oss_contributions
    from gittensor.validator.utils.load_weights import (
        load_master_repo_weights,
        load_programming_language_weights,
        load_token_config,
    )

    if not pat:
        _error("--pat flag or GITTENSOR_MINER_PAT environment variable is required.", False)
        sys.exit(1)

    getattr(bt.logging, f"set_{log_level}")()

    stub = LocalValidatorStub()
    miner_uids = {PIPELINE_DEV_UID}

    with console.status("[bold cyan]Loading weights…", spinner="dots"):
        master_repositories = load_master_repo_weights()
        programming_languages = load_programming_language_weights()
        token_config = load_token_config()

    pat_snapshot = [{"uid": PIPELINE_DEV_UID, "hotkey": PIPELINE_DEV_HOTKEY, "pat": pat}]

    async def _run():
        with override_pats_file(pat_snapshot):
            miner_evaluations, _, _ = await oss_contributions(
                cast(Any, stub), miner_uids, master_repositories, programming_languages, token_config
            )
            await issue_discovery(miner_evaluations, master_repositories, programming_languages, token_config)
        maintainer_uids_by_repo = build_maintainer_uids_by_repo(
            miner_evaluations, master_repositories, miner_uids
        )
        rewards = blend_emission_pools(
            miner_evaluations, master_repositories, miner_uids, maintainer_uids_by_repo
        )
        return miner_evaluations[PIPELINE_DEV_UID], float(rewards[0])

    with console.status("[bold cyan]Running validator pipeline…", spinner="dots"):
        miner_eval, reward = asyncio.run(_run())

    drain_logs()

    advice = _analyse(miner_eval, master_repositories, {"blended_final": reward})
    _render_advice(miner_eval=miner_eval, advice_list=advice, reward=reward)
