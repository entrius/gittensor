# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``gitt challenge``: the optimization challenges.

gitt challenge eval <module> <challenger_dir> --king DIR --seed-block-hash HEX [--json PATH]
"""

from __future__ import annotations

import importlib
import re
import shutil
import tempfile
from pathlib import Path

import click
from rich.markup import escape
from rich.table import Table

from gittensor.challenges import runner
from gittensor.challenges.head_to_head import Entry, canonical, report, snapshot, solver_sha
from gittensor.cli.help import StyledGroup
from gittensor.cli.helpers import console

SOLVER_DIR = click.Path(exists=True, file_okay=False, path_type=Path)


@click.group(name='challenge', cls=StyledGroup)
def challenge_group():
    """Optimization challenges: run a solver head to head against the king (the current crown)."""


def parse_hash(ctx, param, value: str) -> str:
    value = value.lower().removeprefix('0x')
    if not re.fullmatch('[0-9a-f]+', value):
        raise click.BadParameter(f'{value!r} is not hex', ctx, param)
    return value


@challenge_group.command('eval')
@click.argument('module')
@click.argument('challenger_dir', type=SOLVER_DIR)
@click.option('--king', 'king_dir', type=SOLVER_DIR, required=True, help='The solver holding the crown.')
@click.option(
    '--tier', default='standard', show_default=True, help='Challenge tier (its size, time and memory limits).'
)
@click.option('--seeds', type=click.IntRange(min=1), default=1000, metavar='N', show_default=True, help='Instances.')
@click.option('--seed-block-hash', required=True, callback=parse_hash, help='Block hash the seeds derive from (hex).')
@click.option('--margin', type=float, default=0.01, show_default=True, help='Gain the 99% lower bound must reach.')
@click.option('--json', 'json_path', type=click.Path(dir_okay=False, path_type=Path), help='Write the canonical JSON.')
def eval_command(module, challenger_dir, king_dir, tier, seeds, seed_block_hash, margin, json_path):
    """Run CHALLENGER_DIR (an executable `solve`) head to head against the --king solver on challenge MODULE.

    [dim]MODULE is the package's import name (gt_challenge_intents). Seed i is sha256('<hash>:i'); each instance is
    generated once and both solvers run `./solve <instance_dir> <output_dir>` on it in turn, sandboxed under the
    tier's limits. A timeout, crash or invalid output scores 0. The challenger takes the crown when every one of its
    seeds is valid and the 99% lower bound of its mean gain over the king is at least the margin.[/dim]
    """
    try:
        challenge = importlib.import_module(module)
    except ImportError as e:
        raise click.ClickException(f'cannot import {module}: {e}') from e
    if tier not in challenge.TIERS:
        raise click.BadParameter(f'{tier!r} is not one of {", ".join(challenge.TIERS)}', param_hint='--tier')
    if error := runner.sandbox_error():
        raise click.ClickException(f'no sandbox here ({error}): nothing was run')
    with tempfile.TemporaryDirectory(prefix='gt-snapshot-') as private:
        try:
            dirs = [snapshot(challenger_dir, Path(private, 'challenger')), snapshot(king_dir, Path(private, 'king'))]
        except (OSError, shutil.Error) as e:
            raise click.ClickException(f'cannot copy the solvers: {e}') from e
        shas = [solver_sha(d) for d in dirs]
        results = runner.evaluate(challenge, tier, seed_block_hash, seeds, dirs)
    challenger, king = (Entry(sha, r) for sha, r in zip(shas, results))
    try:
        doc = report(module, challenge, tier, seed_block_hash, margin, challenger, king)
        text = canonical(doc)
    except ValueError as e:  # a non-finite mean, gain or bound: nothing is written
        raise click.ClickException(f'cannot report: {e}') from e
    if json_path:
        json_path.write_text(text)

    title = f'{doc["challenge_id"]} {doc["version"]} · tier {tier} · {seeds} seeds · block {seed_block_hash}'
    table = Table(title=f'{title} · {doc["cpus"]} CPUs')
    for column in ('solver', 'sha', 'valid', 'mean', 'first failure'):
        table.add_column(column)
    for name, entry in (('challenger', challenger), ('king', king)):
        failure = next((r.reason for r in entry.results if not r.valid), '')
        row = doc[name]
        table.add_row(name, entry.sha[:12], f'{row["valid"]}/{seeds}', f'{row["mean"]:.6g}', escape(failure))
    console.print(table)
    gain = 'none (the king scored 0)' if doc['mean_gain'] is None else f'{doc["mean_gain"]:+.2%}'
    lower = '' if doc['lower_99'] is None else f' · 99% lower bound {doc["lower_99"]:+.2%}'
    console.print(f'gain {gain}{lower} · margin {margin:.2%} · [bold]crown: {"yes" if doc["crown"] else "no"}[/bold]')


def register_challenge_commands(cli):
    """Register `gitt challenge` with the root CLI group."""
    cli.add_command(challenge_group, name='challenge')
