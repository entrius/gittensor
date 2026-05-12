# Entrius 2025

"""gitt miner score - Run the validator scoring pipeline end-to-end against a single miner.

Stubs axon, wallet, subtensor and DB.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, Iterator, NoReturn, Optional, Set, Tuple, cast

import click
from rich.console import Console
from rich.table import Table

from gittensor.cli.json_output import emit_json
from gittensor.cli.miner_commands.helpers import _error

if TYPE_CHECKING:
    from neurons.validator import Validator

console = Console()

_DEV_UID = 1
_DEV_HOTKEY = 'dev'


def _die(msg: str, json_mode: bool) -> NoReturn:
    _error(msg, json_mode)
    sys.exit(1)


def _resolve_pat(cli_pat: Optional[str], json_mode: bool) -> str:
    if not cli_pat:
        _die('--pat flag or GITTENSOR_MINER_PAT environment variable is required.', json_mode)
    return cli_pat


def _round(x: float) -> float:
    return round(x, 4)


class _StubValidator:
    """Stub `self` with no tensor, wallet, axon, wandb, and DB connection."""

    def __init__(self, uid: int, hotkey: str):
        self.metagraph = SimpleNamespace(hotkeys={uid: hotkey})

    def store_or_use_cached_evaluation(self, miner_evaluations: Dict) -> Set[int]:
        return set()


_EVAL_SKIP: frozenset = frozenset(
    {
        'evaluation_timestamp',
        'merged_prs',
        'open_prs',
        'closed_prs',
        'unique_repos_contributed_to',
    }
)

# @property accessors stored as columns in BULK_UPSERT_MINER_EVALUATION; pulled
# in alongside dataclass fields so JSON keys match DB schema.
_EVAL_PROPERTIES: Tuple[str, ...] = ('total_merged_prs', 'total_open_prs', 'total_closed_prs', 'total_prs')


def _project(obj: Any, skip: frozenset = frozenset(), extra_properties: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """Shallow-project a dataclass into a JSON-friendly dict.

    Coerces Enum -> .value so DB-shape keys carry the underlying string;
    datetime/set fall through to emit_json's default=str at serialize time.
    """

    def _coerce(v: Any) -> Any:
        return v.value if isinstance(v, Enum) else v

    out = {f.name: _coerce(getattr(obj, f.name)) for f in dataclasses.fields(obj) if f.name not in skip}
    out.update({p: _coerce(getattr(obj, p)) for p in extra_properties})
    return out


def _serialize_pr(scored) -> Dict[str, Any]:
    """Flatten a ScoredPR into a JSON-friendly dict, lifting raw fields off .pr."""
    payload = _project(scored, skip=frozenset({'pr', 'files'}))
    payload['repository_full_name'] = scored.pr.repo_full_name
    payload['number'] = scored.pr.pr_number
    payload['pr_state'] = scored.pr.state
    return payload


def _serialize_evaluation(miner_eval) -> Dict[str, Any]:
    payload = _project(miner_eval, skip=_EVAL_SKIP, extra_properties=_EVAL_PROPERTIES)
    payload['merged_pull_requests'] = [_serialize_pr(s) for s in miner_eval.merged_prs]
    payload['open_pull_requests'] = [_serialize_pr(s) for s in miner_eval.open_prs]
    payload['closed_pull_requests'] = [_serialize_pr(s) for s in miner_eval.closed_prs]
    return payload


def _render_table(payload: Dict[str, Any]) -> None:
    miner = payload['miner_evaluation']
    rewards = payload['rewards']

    table = Table(title=f'Miner UID {miner["uid"]} ({miner["hotkey"]}) - github_id={miner["github_id"]}')
    table.add_column('Field', style='cyan')
    table.add_column('Value', style='green')

    if miner['failed_reason']:
        table.add_row('[red]failed_reason[/red]', miner['failed_reason'])
        console.print(table)
        return

    table.add_row('Eligible (OSS)', str(miner['is_eligible']))
    table.add_row('Credibility (OSS)', f'{miner["credibility"]:.4f}')
    table.add_row('Eligible (issue discovery)', str(miner['is_issue_eligible']))
    table.add_row('Credibility (issue)', f'{miner["issue_credibility"]:.4f}')
    table.add_row(
        'PRs merged / open / closed',
        f'{miner["total_merged_prs"]} / {miner["total_open_prs"]} / {miner["total_closed_prs"]}',
    )
    table.add_row('Unique repos', str(miner['unique_repos_count']))
    table.add_row('Total token score', f'{miner["total_token_score"]:.2f}')
    table.add_row('Base total score', f'{miner["base_total_score"]:.2f}')
    table.add_row('[bold]Total earned score[/bold]', f'[bold]{miner["total_score"]:.2f}[/bold]')
    table.add_row('Total collateral', f'{miner["total_collateral_score"]:.2f}')
    table.add_row('Issue discovery score', f'{miner["issue_discovery_score"]:.2f}')
    table.add_row(
        '  solved / valid / open',
        f'{miner["total_solved_issues"]} / {miner["total_valid_solved_issues"]} / {miner["total_open_issues"]}',
    )
    table.add_row('OSS reward (normalized)', f'{rewards["oss_normalized"]:.6f}')
    table.add_row('Issue disc. reward (normalized)', f'{rewards["issue_discovery_normalized"]:.6f}')
    table.add_row(
        '[bold green]Final blended reward[/bold green]', f'[bold green]{rewards["blended_final"]:.6f}[/bold green]'
    )
    console.print(table)

    pr_table = Table(title='Per-PR breakdown', show_lines=False)
    pr_table.add_column('Repo#PR', style='cyan')
    pr_table.add_column('State', style='magenta')
    pr_table.add_column('Base', justify='right')
    pr_table.add_column('Earned', justify='right')
    pr_table.add_column('Token', justify='right')
    pr_table.add_column('Label')

    def _add(prs) -> None:
        for pr in prs:
            pr_table.add_row(
                f'{pr["repository_full_name"]}#{pr["number"]}',
                pr['pr_state'],
                f'{pr["base_score"]:.2f}',
                f'{pr["earned_score"]:.2f}',
                f'{pr["token_score"]:.2f}',
                pr['label'] or '-',
            )

    _add(miner['merged_pull_requests'] + miner['open_pull_requests'] + miner['closed_pull_requests'])
    if pr_table.row_count > 0:
        console.print(pr_table)


@contextmanager
def _override_pats_file(snapshot: list) -> Iterator[None]:
    """Point pat_storage at a tempfile holding our injected PAT snapshot."""
    from gittensor.validator import pat_storage

    original = pat_storage.PATS_FILE
    with TemporaryDirectory() as tmp:
        fake = Path(tmp) / 'miner_pats.json'
        fake.write_text(json.dumps(snapshot))
        pat_storage.PATS_FILE = fake
        try:
            yield
        finally:
            pat_storage.PATS_FILE = original


def _apply_log_level(level: str) -> None:
    """Configure bittensor's logger. The dev tool bypasses BaseNeuron, which is
    where production normally calls `bt.logging.set_config`, so the level stays
    at the default (warning) unless we set it ourselves.
    """
    import bittensor as bt

    getattr(bt.logging, f'set_{level}')()


def _drain_logs() -> None:
    """Stop log production and wait for the async stderr writer to drain.

    bittensor logs through a queue consumed by a background worker thread.
    `queue.empty()` only tells us nothing more is *enqueued*; the worker may
    still be inside `write()` for the last record. The grace tick covers that
    race so the final lines aren't truncated by a fast process exit.
    """
    import time

    import bittensor as bt

    bt.logging.off()
    queue = bt.logging.get_queue()
    if not queue.empty():
        deadline = time.monotonic() + 2.0
        while not queue.empty() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.1)
    sys.stderr.flush()
    sys.stdout.flush()


@click.command(name='score')
@click.option(
    '--pat',
    default=None,
    envvar='GITTENSOR_MINER_PAT',
    help='GitHub Personal Access Token. Uses GITTENSOR_MINER_PAT env if unset.',
)
@click.option(
    '--log-level',
    type=click.Choice(['warning', 'info', 'debug', 'trace']),
    default='info',
    show_default=True,
    help="Bittensor log verbosity. 'info' surfaces the validator pipeline's per-step progress on stderr.",
)
@click.option('--json', 'json_mode', is_flag=True, default=False, help='Emit result as JSON on stdout.')
def score_command(pat: Optional[str], log_level: str, json_mode: bool) -> None:
    """Locally run the validator scoring pipeline end-to-end for the miner identified by --pat.

    No subtensor, wallet, DB, axon, or wandb is touched.

    Example:
        gitt miner score --pat ghp_xxxxx
        gitt miner score --pat ghp_xxxxx --log-level debug
    """
    import asyncio

    resolved_pat = _resolve_pat(pat, json_mode)

    # Deferred imports: keeps --help fast (these pull bittensor + the validator graph).
    from gittensor.validator.forward import (
        blend_emission_pools,
        issue_discovery,
        oss_contributions,
    )
    from gittensor.validator.utils.load_weights import (
        load_master_repo_weights,
        load_programming_language_weights,
        load_token_config,
    )

    _apply_log_level(log_level)

    # `oss_contributions` is typed as taking a real `Validator`; the stub fulfils
    # the surface that function actually uses (metagraph.hotkeys, the cache hook).
    stub = cast('Validator', _StubValidator(_DEV_UID, _DEV_HOTKEY))
    miner_uids = {_DEV_UID}

    if json_mode:
        master_repositories = load_master_repo_weights()
        programming_languages = load_programming_language_weights()
        token_config = load_token_config()
    else:
        with console.status('[bold cyan]Loading weights', spinner='dots'):
            master_repositories = load_master_repo_weights()
            programming_languages = load_programming_language_weights()
            token_config = load_token_config()

    pat_snapshot = [{'uid': _DEV_UID, 'hotkey': _DEV_HOTKEY, 'pat': resolved_pat}]

    async def _run() -> Dict[str, Any]:
        with _override_pats_file(pat_snapshot):
            oss_rewards, miner_evaluations, _, _ = await oss_contributions(
                stub, miner_uids, master_repositories, programming_languages, token_config
            )
            issue_rewards = await issue_discovery(
                miner_evaluations, master_repositories, programming_languages, token_config, miner_uids
            )
        rewards = blend_emission_pools(miner_uids, miner_evaluations, master_repositories)

        return {
            'success': True,
            'miner_evaluation': _serialize_evaluation(miner_evaluations[_DEV_UID]),
            'rewards': {
                'oss_normalized': _round(float(oss_rewards[0])),
                'issue_discovery_normalized': _round(float(issue_rewards[0])),
                'blended_final': _round(float(rewards[0])),
            },
        }

    if not json_mode:
        console.print('[bold cyan]Running validator pipeline...[/bold cyan]')
    payload = asyncio.run(_run())

    _drain_logs()

    if json_mode:
        emit_json(payload)
    else:
        _render_table(payload)
