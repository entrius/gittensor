# Entrius 2025

"""gitt miner advisor - Run the validator scoring pipeline locally and turn the
result into prioritized, implementable recommendations.

Reuses the local-pipeline stubs from ``gitt miner score`` (no subtensor, wallet,
DB, axon, or wandb is touched) and analyzes the resulting evaluation against the
live eligibility/scoring gates, surfacing advice by impact level:

    CRITICAL  blockers that prevent the miner from being eligible to earn
    WARNING   active reductions shrinking an otherwise-earned score
    TIP       unused multipliers that would raise the score
    INFO      planning context (totals, distance to thresholds, reward)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast

import click
from rich.console import Console
from rich.table import Table

from gittensor.cli.json_output import emit_json
from gittensor.cli.miner_commands.score import (
    _DEV_HOTKEY,
    _DEV_UID,
    _StubValidator,
    _apply_log_level,
    _drain_logs,
    _override_pats_file,
    _resolve_pat,
    _serialize_evaluation,
)

if TYPE_CHECKING:
    from neurons.validator import Validator

console = Console()


class Impact(str, Enum):
    CRITICAL = 'CRITICAL'
    WARNING = 'WARNING'
    TIP = 'TIP'
    INFO = 'INFO'


_IMPACT_ORDER = {Impact.CRITICAL: 0, Impact.WARNING: 1, Impact.TIP: 2, Impact.INFO: 3}
_IMPACT_STYLE = {
    Impact.CRITICAL: 'bold red',
    Impact.WARNING: 'yellow',
    Impact.TIP: 'cyan',
    Impact.INFO: 'dim',
}


@dataclass
class Recommendation:
    impact: Impact
    title: str
    detail: str
    repo: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {'impact': self.impact.value, 'title': self.title, 'detail': self.detail, 'repo': self.repo}


def _analyze_eligibility(miner: Dict[str, Any], thresholds: Dict[str, Dict[str, float]]) -> List[Recommendation]:
    """CRITICAL: per-repo eligibility blockers (too few merged PRs, low credibility)."""
    recs: List[Recommendation] = []
    repo_evals = miner.get('repo_evaluations') or {}
    for repo, re in sorted(repo_evals.items()):
        if re.get('is_eligible'):
            continue
        limits = thresholds.get(repo, {})
        merged = int(re.get('total_merged_prs', 0))
        need_merged = int(limits.get('min_valid_merged_prs', 0))
        cred = float(re.get('credibility', 0.0))
        need_cred = float(limits.get('min_credibility', 0.0))
        reasons = []
        if merged < need_merged:
            reasons.append(f'{merged}/{need_merged} valid merged PRs')
        if cred < need_cred:
            reasons.append(f'credibility {cred:.2f} < {need_cred:.2f} (raise your merged:closed ratio)')
        recs.append(
            Recommendation(
                Impact.CRITICAL,
                f'Not eligible in {repo}',
                '; '.join(reasons) or 'eligibility gate not met',
                repo=repo,
            )
        )
    return recs


def _analyze_reductions(miner: Dict[str, Any]) -> List[Recommendation]:
    """WARNING: multipliers below 1.0 that are actively shrinking earned score."""
    recs: List[Recommendation] = []
    for pr in miner.get('merged_pull_requests', []):
        tag = f'{pr["repository_full_name"]}#{pr["number"]}'
        if float(pr.get('review_quality_multiplier', 1.0)) < 1.0:
            recs.append(
                Recommendation(
                    Impact.WARNING,
                    f'Review penalty on {tag}',
                    f'maintainer change-requests cut this PR to '
                    f'×{pr["review_quality_multiplier"]:.2f}; address review feedback before merge',
                    repo=pr['repository_full_name'],
                )
            )
        if float(pr.get('open_pr_spam_multiplier', 1.0)) == 0.0:
            recs.append(
                Recommendation(
                    Impact.WARNING,
                    f'Open-PR spam penalty zeroed {tag}',
                    'too many open PRs in this repo zeroed its merged scores; close or land open PRs',
                    repo=pr['repository_full_name'],
                )
            )
        if float(pr.get('time_decay_multiplier', 1.0)) < 0.5:
            recs.append(
                Recommendation(
                    Impact.WARNING,
                    f'Time decay on {tag}',
                    f'this merge has decayed to ×{pr["time_decay_multiplier"]:.2f}; recent merges score higher',
                    repo=pr['repository_full_name'],
                )
            )
    collateral = float(miner.get('total_collateral_score', 0.0))
    if collateral > 0:
        recs.append(
            Recommendation(
                Impact.WARNING,
                'Open PRs are reserving score as collateral',
                f'{collateral:.2f} pts held against open PRs; merging or closing them releases it',
            )
        )
    return recs


def _analyze_tips(miner: Dict[str, Any]) -> List[Recommendation]:
    """TIP: unused multipliers (issue links, labels) that would raise score."""
    recs: List[Recommendation] = []
    no_issue = [
        f'{pr["repository_full_name"]}#{pr["number"]}'
        for pr in miner.get('merged_pull_requests', [])
        if float(pr.get('issue_multiplier', 1.0)) <= 1.0
    ]
    if no_issue:
        recs.append(
            Recommendation(
                Impact.TIP,
                'Link a valid issue for the issue multiplier',
                f'{len(no_issue)} merged PR(s) earned no issue multiplier '
                f'({", ".join(no_issue[:5])}{"..." if len(no_issue) > 5 else ""}). '
                'Solve a maintainer-filed issue and link it with "Fixes #N".',
            )
        )
    weak_label = [
        f'{pr["repository_full_name"]}#{pr["number"]}'
        for pr in miner.get('merged_pull_requests', [])
        if float(pr.get('label_multiplier', 1.0)) < 1.0
    ]
    if weak_label:
        recs.append(
            Recommendation(
                Impact.TIP,
                'Low-value labels are capping score',
                f'{len(weak_label)} PR(s) carry a sub-1.0 label multiplier '
                f'({", ".join(weak_label[:5])}{"..." if len(weak_label) > 5 else ""}); '
                'target higher-weighted labels for this repo.',
            )
        )
    return recs


def _analyze_info(miner: Dict[str, Any], rewards: Dict[str, Any]) -> List[Recommendation]:
    recs: List[Recommendation] = [
        Recommendation(
            Impact.INFO,
            'Current standing',
            f'earned score {float(miner.get("total_score", 0.0)):.2f} across '
            f'{int(miner.get("unique_repos_count", 0))} repo(s); '
            f'blended reward {float(rewards.get("blended_final", 0.0)):.6f}',
        )
    ]
    return recs


def build_recommendations(payload: Dict[str, Any], thresholds: Dict[str, Dict[str, float]]) -> List[Recommendation]:
    miner = payload['miner_evaluation']
    if miner.get('failed_reason'):
        return [Recommendation(Impact.CRITICAL, 'Evaluation failed', str(miner['failed_reason']))]
    recs: List[Recommendation] = []
    recs += _analyze_eligibility(miner, thresholds)
    recs += _analyze_reductions(miner)
    recs += _analyze_tips(miner)
    recs += _analyze_info(miner, payload.get('rewards', {}))
    recs.sort(key=lambda r: _IMPACT_ORDER[r.impact])
    return recs


def _render(recs: List[Recommendation]) -> None:
    table = Table(title='Miner advisor — prioritized recommendations')
    table.add_column('Impact', style='bold')
    table.add_column('Recommendation')
    table.add_column('Detail', overflow='fold')
    for r in recs:
        table.add_row(f'[{_IMPACT_STYLE[r.impact]}]{r.impact.value}[/]', r.title, r.detail)
    console.print(table)


def _resolve_thresholds() -> Dict[str, Dict[str, float]]:
    """Resolve each whitelisted repo's eligibility gate to concrete numbers."""
    from gittensor.validator.utils.load_weights import load_master_repo_weights, resolve_eligibility

    out: Dict[str, Dict[str, float]] = {}
    for repo, cfg in load_master_repo_weights().items():
        elig = resolve_eligibility(cfg.eligibility)
        out[repo] = {
            'min_valid_merged_prs': elig.min_valid_merged_prs,
            'min_credibility': elig.min_credibility,
        }
    return out


@click.command(name='advisor')
@click.option('--pat', default=None, envvar='GITTENSOR_MINER_PAT', help='GitHub PAT. Uses GITTENSOR_MINER_PAT if unset.')
@click.option(
    '--log-level',
    type=click.Choice(['warning', 'info', 'debug', 'trace']),
    default='warning',
    show_default=True,
    help='Bittensor log verbosity for the underlying pipeline run.',
)
@click.option('--json', 'json_mode', is_flag=True, default=False, help='Emit recommendations as JSON on stdout.')
def advisor_command(pat: Optional[str], log_level: str, json_mode: bool) -> None:
    """Run the scoring pipeline locally and report prioritized recommendations.

    Example:
        gitt miner advisor --pat ghp_xxxxx
    """
    resolved_pat = _resolve_pat(pat, json_mode)

    from gittensor.validator.emission_allocation import blend_emission_pools
    from gittensor.validator.forward import build_maintainer_uids_by_repo, issue_discovery, oss_contributions
    from gittensor.validator.utils.load_weights import (
        load_master_repo_weights,
        load_programming_language_weights,
        load_token_config,
    )

    _apply_log_level(log_level)
    stub = cast('Validator', _StubValidator(_DEV_UID, _DEV_HOTKEY))
    miner_uids = {_DEV_UID}
    master_repositories = load_master_repo_weights()
    programming_languages = load_programming_language_weights()
    token_config = load_token_config()
    pat_snapshot = [{'uid': _DEV_UID, 'hotkey': _DEV_HOTKEY, 'pat': resolved_pat}]

    async def _run() -> Dict[str, Any]:
        with _override_pats_file(pat_snapshot):
            miner_evaluations, _, _ = await oss_contributions(
                stub, miner_uids, master_repositories, programming_languages, token_config
            )
            await issue_discovery(miner_evaluations, master_repositories, programming_languages, token_config)
        maintainer_uids_by_repo = build_maintainer_uids_by_repo(miner_evaluations, master_repositories, miner_uids)
        rewards = blend_emission_pools(miner_evaluations, master_repositories, miner_uids, maintainer_uids_by_repo)
        return {
            'miner_evaluation': _serialize_evaluation(miner_evaluations[_DEV_UID]),
            'rewards': {'blended_final': float(rewards[0])},
        }

    if not json_mode:
        console.print('[bold cyan]Running validator pipeline...[/bold cyan]')
    payload = asyncio.run(_run())
    _drain_logs()

    recommendations = build_recommendations(payload, _resolve_thresholds())

    if json_mode:
        emit_json({'success': True, 'recommendations': [r.to_dict() for r in recommendations]})
    else:
        _render(recommendations)
