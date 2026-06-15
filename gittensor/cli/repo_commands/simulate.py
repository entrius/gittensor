# Entrius 2025

"""gitt repo simulate - Preview the emission/reward split for a proposed
master_repositories.json without committing a reweight.

Read-only "what-if": feeds synthetic per-miner, per-repo scores through the
real emission-allocation functions (``calculate_repo_emission_breakdown`` and
``blend_emission_pools``) so the simulated split matches production exactly. No
subtensor, wallet, DB, PAT, or network is touched - the only thing that varies
is the weights config and the injected scores.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, Dict, List, NoReturn, Optional, Set, Tuple

import click
from rich.console import Console
from rich.table import Table

from gittensor.cli.issue_commands.help import StyledCommand
from gittensor.cli.json_output import emit_json
from gittensor.cli.miner_commands.helpers import _error

if TYPE_CHECKING:
    import numpy as np

    from gittensor.classes import MinerEvaluation, RepoEmissionAllocation
    from gittensor.validator.utils.load_weights import RepositoryConfig

console = Console()
err_console = Console(stderr=True)

# Synthetic miners carry a placeholder hotkey; the allocation functions only key
# off the UID, so the value is cosmetic.
_SIM_HOTKEY_PREFIX = 'sim-'


def _die(msg: str, json_mode: bool) -> NoReturn:
    _error(msg, json_mode)
    sys.exit(1)


def _as_float(value: Any, what: str, json_mode: bool) -> float:
    # bool is an int subclass; reject it so `true` cannot masquerade as 1.0.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _die(f'{what} must be a number, got {value!r}', json_mode)
    return float(value)


def _load_json_file(path: str, what: str, json_mode: bool) -> Any:
    file_path = Path(path)
    if not file_path.is_file():
        _die(f'{what} file not found: {path}', json_mode)
    try:
        return json.loads(file_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as e:
        _die(f'{what} could not be read as JSON ({path}): {e}', json_mode)


def _parse_maintainers(raw: Any, json_mode: bool) -> Dict[str, List[int]]:
    """Parse the optional ``maintainers`` map (repo -> list of maintainer UIDs).

    Passed straight to the allocation functions as ``maintainer_uids_by_repo`` -
    the exact shape production builds from the mirror - so a ``maintainer_cut``
    carve-out can be previewed offline.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _die('"maintainers" must map a repository name to a list of UIDs', json_mode)
    result: Dict[str, List[int]] = {}
    for repo_name, uids in raw.items():
        if not isinstance(uids, list) or not all(isinstance(u, int) and not isinstance(u, bool) for u in uids):
            _die(f'maintainers for "{repo_name}" must be a list of integer UIDs', json_mode)
        result[repo_name.lower()] = sorted(set(uids))
    return result


def _build_miner_evaluations(
    scenario: Any, json_mode: bool
) -> Tuple[Dict[int, 'MinerEvaluation'], Dict[str, List[int]], Set[str]]:
    """Turn the scenario JSON into the minimal MinerEvaluation objects the
    allocation functions consume.

    Only three things are read downstream: ``failed_reason`` (left None so the
    eval scores), ``repo_evaluations[repo].total_score`` (the PR side) and
    ``issue_discovery_issues[].discovery_earned_score`` (the issue side), so we
    build only those.
    """
    from gittensor.classes import Issue, MinerEvaluation, RepoEvaluation

    if not isinstance(scenario, dict):
        _die('scenario must be a JSON object', json_mode)
    miners = scenario.get('miners')
    if not isinstance(miners, list) or not miners:
        _die("scenario must contain a non-empty 'miners' array", json_mode)

    evaluations: Dict[int, MinerEvaluation] = {}
    referenced_repos: Set[str] = set()
    issue_counter = 0

    for entry in miners:
        if not isinstance(entry, dict) or 'uid' not in entry:
            _die("each entry in 'miners' must be an object with a 'uid'", json_mode)
        uid = entry['uid']
        if not isinstance(uid, int) or isinstance(uid, bool):
            _die(f'miner uid must be an integer, got {uid!r}', json_mode)
        if uid in evaluations:
            _die(f'duplicate miner uid {uid}', json_mode)

        evaluation = MinerEvaluation(uid=uid, hotkey=f'{_SIM_HOTKEY_PREFIX}{uid}')
        repos = entry.get('repos', {})
        if not isinstance(repos, dict):
            _die(f'miner {uid} "repos" must be an object keyed by repository name', json_mode)

        for repo_name, scores in repos.items():
            if not isinstance(scores, dict):
                _die(f'miner {uid} repository "{repo_name}" must map to an object', json_mode)
            # master_repositories keys are lowercased on load and the allocation
            # collectors match case-insensitively, so normalise here too.
            repo_key = repo_name.lower()
            referenced_repos.add(repo_key)
            pr_score = _as_float(scores.get('pr_score', 0.0), f'miner {uid} {repo_name} pr_score', json_mode)
            issue_score = _as_float(scores.get('issue_score', 0.0), f'miner {uid} {repo_name} issue_score', json_mode)
            if pr_score < 0 or issue_score < 0:
                _die(f'miner {uid} {repo_name} scores must be non-negative', json_mode)

            if pr_score > 0:
                evaluation.repo_evaluations[repo_key] = RepoEvaluation(
                    repository_full_name=repo_key, total_score=pr_score
                )
            if issue_score > 0:
                issue_counter += 1
                evaluation.issue_discovery_issues.append(
                    Issue(
                        number=issue_counter,
                        pr_number=0,
                        repository_full_name=repo_key,
                        title=f'simulated issue {issue_counter}',
                        discovery_earned_score=issue_score,
                    )
                )
        evaluations[uid] = evaluation

    maintainer_uids_by_repo = _parse_maintainers(scenario.get('maintainers'), json_mode)
    return evaluations, maintainer_uids_by_repo, referenced_repos


def _load_master_repositories(path: Optional[str], json_mode: bool) -> Dict[str, 'RepositoryConfig']:
    """Load a master_repositories.json through the real, validated loader.

    With no path the live repo weights are used. With a path we temporarily
    point ``load_master_repo_weights`` at the supplied file (mirroring the
    file-override ``miner score`` uses for PATs) so the proposed config runs
    through the exact same parsing and emission-share validation as production -
    surfacing an invalid reweight before it is ever committed.
    """
    from gittensor.validator.utils import load_weights
    from gittensor.validator.utils.load_weights import RepositoryRegistryError, load_master_repo_weights

    src = 'live weights' if path is None else path
    try:
        if path is None:
            configs = load_master_repo_weights()
        else:
            file_path = Path(path)
            if not file_path.is_file():
                _die(f'config file not found: {path}', json_mode)
            with TemporaryDirectory() as tmp:
                (Path(tmp) / 'master_repositories.json').write_text(
                    file_path.read_text(encoding='utf-8'), encoding='utf-8'
                )
                original = load_weights._get_weights_dir
                load_weights._get_weights_dir = lambda directory=Path(tmp): directory
                try:
                    configs = load_master_repo_weights()
                finally:
                    load_weights._get_weights_dir = original
    except (RepositoryRegistryError, ValueError) as e:
        _die(f'invalid master_repositories config ({src}): {e}', json_mode)

    if not configs:
        _die(f'no repositories loaded from {src}', json_mode)
    return configs


def _uid_role(uid: int) -> Optional[str]:
    from gittensor.constants import ISSUES_TREASURY_UID, RECYCLE_UID

    if uid == RECYCLE_UID:
        return 'recycled'
    if uid == ISSUES_TREASURY_UID:
        return 'issues_treasury'
    return None


def _serialize_allocation(allocation: 'RepoEmissionAllocation') -> Dict[str, Any]:
    """Per-repo emission breakdown (pool totals; per-miner detail is summed)."""
    pr_paid = sum(allocation.pr_rewards.values())
    issue_paid = sum(allocation.issue_discovery_rewards.values())
    maintainer_paid = sum(allocation.maintainer_rewards.values())
    return {
        'repository_full_name': allocation.repository_full_name,
        'emission_share': round(allocation.emission_share, 8),
        'issue_discovery_share': round(allocation.issue_discovery_share, 8),
        'maintainer_cut': round(allocation.maintainer_cut, 8),
        'repo_slice': round(allocation.repo_slice, 8),
        'maintainer_carve_out': round(allocation.maintainer_carve_out, 8),
        'pr_slice': round(allocation.pr_slice, 8),
        'issue_discovery_slice': round(allocation.issue_discovery_slice, 8),
        'pr_paid': round(pr_paid, 8),
        'issue_paid': round(issue_paid, 8),
        'maintainer_paid': round(maintainer_paid, 8),
        'distributed': round(pr_paid + issue_paid + maintainer_paid, 8),
        'recycled_amount': round(allocation.recycled_amount, 8),
        'recycled': allocation.recycled_amount > 1e-12,
    }


def _per_miner_totals(rewards: 'np.ndarray', miner_uids: Set[int]) -> List[Dict[str, Any]]:
    """Map the blended reward vector back to UIDs.

    ``blend_emission_pools`` indexes its output by ``sorted(miner_uids)``, so
    the same ordering recovers each UID's total emission share.
    """
    sorted_uids = sorted(miner_uids)
    return [
        {'uid': uid, 'emission': round(float(rewards[idx]), 8), 'role': _uid_role(uid)}
        for idx, uid in enumerate(sorted_uids)
    ]


def _config_summary(path: Optional[str], configs: Dict[str, 'RepositoryConfig']) -> Dict[str, Any]:
    return {
        'source': 'live' if path is None else path,
        'repo_count': len(configs),
        'total_emission_share': round(sum(c.emission_share for c in configs.values()), 8),
    }


def _collect_warnings(
    referenced_repos: Set[str],
    evaluations: Dict[int, 'MinerEvaluation'],
    configs: Dict[str, 'RepositoryConfig'],
) -> List[str]:
    """Soft checks for the common scenario mistakes (typo'd repo name, UID clash
    with the reserved recycle / treasury slots) - surfaced, not fatal."""
    from gittensor.constants import ISSUES_TREASURY_UID, RECYCLE_UID

    warnings: List[str] = []
    unknown = sorted(referenced_repos - set(configs))
    if unknown:
        warnings.append(
            'scenario references repositories absent from the config (they contribute nothing): ' + ', '.join(unknown)
        )
    reserved = sorted({RECYCLE_UID, ISSUES_TREASURY_UID} & set(evaluations))
    if reserved:
        warnings.append(
            f'scenario uses reserved UIDs {reserved} (0 = recycle, 111 = issues treasury); '
            'their emission stacks on top of the simulated miner'
        )
    return warnings


def _render(payload: Dict[str, Any]) -> None:
    """Render the table view (the JSON view emits the same payload as-is)."""
    summary = payload['config']
    console.print(
        f'[bold]Config:[/bold] [cyan]{summary["source"]}[/cyan]  '
        f'repos=[green]{summary["repo_count"]}[/green]  '
        f'total emission_share=[green]{summary["total_emission_share"]:.6f}[/green]\n'
    )

    repo_table = Table(title='Per-repository emission allocation', show_lines=False)
    repo_table.add_column('Repository', style='cyan')
    repo_table.add_column('Share', justify='right')
    repo_table.add_column('Repo slice', justify='right')
    repo_table.add_column('Maint. paid', justify='right')
    repo_table.add_column('PR paid', justify='right')
    repo_table.add_column('Issue paid', justify='right')
    repo_table.add_column('Recycled', justify='right')
    for row in payload['allocations']:
        repo_table.add_row(
            row['repository_full_name'],
            f'{row["emission_share"]:.4f}',
            f'{row["repo_slice"]:.6f}',
            f'{row["maintainer_paid"]:.6f}' if row['maintainer_paid'] > 0 else '-',
            f'{row["pr_paid"]:.6f}',
            f'{row["issue_paid"]:.6f}',
            f'{row["recycled_amount"]:.6f}' if row['recycled'] else '-',
        )
    console.print(repo_table)

    miner_table = Table(title='Per-miner emission totals', show_lines=False)
    miner_table.add_column('UID', justify='right', style='cyan')
    miner_table.add_column('Emission', justify='right')
    miner_table.add_column('Role', style='magenta')
    for row in payload['miners']:
        miner_table.add_row(str(row['uid']), f'{row["emission"]:.8f}', row['role'] or '-')
    console.print(miner_table)

    for warning in payload['warnings']:
        err_console.print(f'[yellow]warning:[/yellow] {warning}')


@click.command(name='simulate', cls=StyledCommand)
@click.option(
    '--scenario',
    'scenario_path',
    required=True,
    type=click.Path(dir_okay=False, path_type=str),
    help='Path to a scenario JSON describing synthetic miners and their per-repo scores.',
)
@click.option(
    '--config',
    'config_path',
    default=None,
    type=click.Path(dir_okay=False, path_type=str),
    help='Proposed master_repositories.json to simulate. Defaults to the live repo weights.',
)
@click.option('--json', 'json_mode', is_flag=True, default=False, help='Emit result as JSON on stdout.')
def simulate_command(scenario_path: str, config_path: Optional[str], json_mode: bool) -> None:
    """Preview how a master_repositories.json reweight changes the emission split.

    Feeds synthetic per-miner, per-repo scores from --scenario through the real
    emission-allocation functions and reports the resulting per-repo allocation
    and per-miner emission totals. Read-only: no subtensor, wallet, DB, PAT, or
    network is touched. UIDs 0 (recycle) and 111 (issues treasury) are always
    included so the totals account for the full emission.

    \b
    The scenario file looks like:
        {
          "miners": [
            {"uid": 5, "repos": {"entrius/allways": {"issue_score": 12.0}}},
            {"uid": 6, "repos": {"cogniax/tao-pulse-app": {"pr_score": 80.0}}}
          ],
          "maintainers": {"cogniax/tao-pulse-app": [6]}
        }

    pr_score feeds the PR pool, issue_score the issue-discovery pool; either may
    be omitted. The optional "maintainers" map previews a maintainer_cut
    carve-out. Repository names are matched case-insensitively.

    \b
    Examples:
        gitt repo simulate --scenario scenario.json
        gitt repo simulate --scenario scenario.json --config proposed.json --json
    """
    from gittensor.constants import ISSUES_TREASURY_UID, RECYCLE_UID
    from gittensor.validator.emission_allocation import (
        blend_emission_pools,
        calculate_repo_emission_breakdown,
    )

    scenario = _load_json_file(scenario_path, 'scenario', json_mode)
    evaluations, maintainer_uids_by_repo, referenced_repos = _build_miner_evaluations(scenario, json_mode)
    # Recycle (0) and issues-treasury (111) are added so the blended vector
    # accounts for the full emission, matching what production distributes.
    miner_uids = set(evaluations) | {RECYCLE_UID, ISSUES_TREASURY_UID}

    configs = _load_master_repositories(config_path, json_mode)
    allocations = list(calculate_repo_emission_breakdown(evaluations, configs, miner_uids, maintainer_uids_by_repo))
    rewards = blend_emission_pools(evaluations, configs, miner_uids, maintainer_uids_by_repo)

    payload: Dict[str, Any] = {
        'success': True,
        'config': _config_summary(config_path, configs),
        'allocations': [_serialize_allocation(a) for a in allocations],
        'miners': _per_miner_totals(rewards, miner_uids),
        'warnings': _collect_warnings(referenced_repos, evaluations, configs),
    }
    if json_mode:
        emit_json(payload)
    else:
        _render(payload)
