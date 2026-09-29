# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``gitt challenge``: the challenge pool.

gitt challenge eval <module> <solver_dir> [--tier --seeds --public-seed]   score a solver locally, per seed
gitt challenge submit <challenge_id> <solver_dir> [--out DIR] [--commit]   bundle, commit on chain, upload or save
gitt challenge round --candidates <json> --public-seed <s> --state-dir <dir> [--key]   the evaluator: one round
"""

from __future__ import annotations

import importlib
import json
import secrets
import time
from dataclasses import asdict
from pathlib import Path

import click
from rich.markup import escape
from rich.table import Table

from gittensor.challenges.attest import DevAttestor
from gittensor.challenges.evaluator import Candidate, SandboxUnavailable, run_round
from gittensor.challenges.registry import REGISTRY_PATH, RegistryError, load_registry
from gittensor.challenges.runner import evaluate
from gittensor.challenges.submission import SubmissionError, build_bundle, hippius_credentials, upload
from gittensor.cli.help import StyledGroup
from gittensor.cli.helpers import NETWORK_CHOICE, console, err_console
from gittensor.cli.json_output import emit_json
from gittensor.cli.miner_commands.helpers import NETUID_DEFAULT, _load_config_value, _resolve_endpoint


@click.group(name='challenge', cls=StyledGroup)
def challenge_group():
    """Optimization challenges: score a solver locally, then submit it."""


@challenge_group.command('eval')
@click.argument('module')
@click.argument('solver_dir', type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option('--tier', default='small', show_default=True, help='Challenge tier (its size, time and memory limits).')
@click.option(
    '--seeds', type=click.IntRange(min=1), default=8, metavar='N', show_default=True, help='Instances to run.'
)
@click.option('--public-seed', default=None, help='Seed string the instances derive from (default: random).')
@click.option('--json', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def eval_command(module, solver_dir, tier, seeds, public_seed, json_mode):
    """Score SOLVER_DIR (an executable `solve`) locally against the challenge package MODULE.

    [dim]MODULE is the package's import name (gt_challenge_routing). Each seed: generate an instance, run
    `./solve <instance_dir> <output_dir>` under the tier's limits, check. A timeout, crash or invalid output scores 0;
    the score is the mean.[/dim]
    """
    try:
        challenge = importlib.import_module(module)
    except ImportError as e:
        raise click.ClickException(f'cannot import {module}: {e}') from e
    if tier not in challenge.TIERS:
        raise click.BadParameter(f'{tier!r} is not one of {", ".join(challenge.TIERS)}', param_hint='--tier')
    public_seed = public_seed or secrets.token_hex(16)
    result = evaluate(challenge, solver_dir, tier, public_seed, seeds)
    if json_mode:
        emit_json({**asdict(result), 'score': result.score})
        return
    table = Table(title=f'{result.challenge_id} {result.version} · tier {tier} · public seed {public_seed}')
    for column in ('seed', 'valid', 'score', 'seconds', 'reason'):
        table.add_column(column)
    for r in result.results:
        table.add_row(r.seed[:12], 'yes' if r.valid else 'no', f'{r.score:.4f}', f'{r.seconds:.2f}', escape(r.reason))
    console.print(table)
    console.print(f'[bold]score {result.score:.4f}[/bold] (mean of {seeds})')


@challenge_group.command('submit')
@click.argument('challenge_id')
@click.argument('solver_dir', type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    '--out',
    type=click.Path(file_okay=False, path_type=Path),
    help='Where to write the bundle when HIPPIUS_* is not set.',
)
@click.option('--commit', is_flag=True, default=False, help='Commit the bundle sha256 on chain with your hotkey.')
@click.option('--registry', type=click.Path(dir_okay=False, path_type=Path), default=REGISTRY_PATH, hidden=True)
@click.option('--wallet', 'wallet_name', default=None, help='Bittensor wallet name.')
@click.option('--hotkey', 'wallet_hotkey', default=None, help='Bittensor hotkey name.')
@click.option('--netuid', type=int, default=NETUID_DEFAULT, help='Subnet UID.', show_default=True)
@click.option('--network', type=NETWORK_CHOICE, default=None, help='Network name (local, test, finney).')
@click.option('--rpc-url', default=None, help='Subtensor RPC endpoint URL (overrides --network).')
@click.option('--json', 'json_mode', is_flag=True, default=False, help='Output results as JSON.')
def submit_command(
    challenge_id, solver_dir, out, commit, registry, wallet_name, wallet_hotkey, netuid, network, rpc_url, json_mode
):
    """Bundle SOLVER_DIR for CHALLENGE_ID, commit it on chain (--commit), then upload it.

    [dim]The bundle goes to Hippius S3 when HIPPIUS_ACCESS_KEY, HIPPIUS_SECRET_KEY and HIPPIUS_BUCKET are set
    (HIPPIUS_ENDPOINT, HIPPIUS_REGION optional), else to --out. The commitment is gt-challenge:<id>:<sha256>;
    within the margin the earlier commit reigns. The upload always follows the commit (a v0 bundle is plaintext:
    one uploaded first can be committed first by anyone), so Hippius needs --commit.[/dim]
    """
    try:
        if challenge_id not in load_registry(registry):
            raise click.BadParameter(f'{challenge_id!r} is not in {registry}', param_hint='CHALLENGE_ID')
        bundle = build_bundle(challenge_id, solver_dir)
    except (RegistryError, SubmissionError) as e:
        raise click.ClickException(str(e)) from e
    creds = hippius_credentials()
    if creds and not commit:
        raise click.UsageError('uploading to Hippius needs --commit: the commit must come first')
    if not creds and not out:
        raise click.UsageError('set HIPPIUS_ACCESS_KEY, HIPPIUS_SECRET_KEY and HIPPIUS_BUCKET, or pass --out')
    committed = commit and _commit(bundle.commitment, wallet_name, wallet_hotkey, netuid, network, rpc_url)
    if creds:
        try:
            location = upload(bundle, creds)
        except OSError as e:
            raise click.ClickException(f'upload to Hippius failed: {e}') from e
    else:
        assert out is not None
        out.mkdir(parents=True, exist_ok=True)
        (out / bundle.name).write_bytes(bundle.data)
        location = str(out / bundle.name)
    if json_mode:
        payload = {'sha256': bundle.sha256, 'commitment': bundle.commitment, 'location': location}
        emit_json({**payload, 'committed': committed})
        return
    err_console.print(f'[green]Bundle[/green] {bundle.sha256} -> {escape(location)}')
    console.print(bundle.commitment)
    if committed:
        err_console.print('[green]Committed on chain.[/green]')
    elif not commit:
        err_console.print('[dim]Not committed; pass --commit to commit it with your hotkey.[/dim]')


@challenge_group.command('round')
@click.option(
    '--candidates',
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help='JSON list of {challenge_id, hotkey, commit_block, submission_sha256, solver_dir}.',
)
@click.option('--public-seed', required=True, help='Seed string the instances derive from (a block hash).')
@click.option(
    '--state-dir',
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help='Holds leaderboard.json, scorecard/ and, by default, the dev key.',
)
@click.option(
    '--key',
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help='ed25519 seed file for the dev attestation (created if missing; default <state-dir>/evaluator_ed25519).',
)
@click.option('--registry', type=click.Path(dir_okay=False, path_type=Path), default=REGISTRY_PATH, hidden=True)
def round_command(candidates, public_seed, state_dir, key, registry):
    """Evaluate the candidates, update the leaderboard, and write the dev-attested scorecard.

    [dim]Prints the scorecard's path and sha256. v0: the dev attestation only proves which key wrote it.[/dim]
    """
    try:
        found = [Candidate(**{**c, 'solver_dir': Path(c['solver_dir'])}) for c in json.loads(candidates.read_text())]
    except (ValueError, TypeError, KeyError) as e:
        raise click.ClickException(f'{candidates}: {e!r}') from e
    try:
        challenges = load_registry(registry)
    except RegistryError as e:
        raise click.ClickException(str(e)) from e
    attestor = DevAttestor(key or state_dir / 'evaluator_ed25519')
    try:
        path, sha = run_round(
            challenges,
            found,
            public_seed,
            state_dir / 'leaderboard.json',
            state_dir / 'scorecard',
            attestor,
            time.time(),
        )
    except SandboxUnavailable as e:
        raise click.ClickException(f'no sandbox here ({e}): nothing was scored or written') from e
    click.echo(f'{path} {sha}')


def _commit(commitment, wallet_name, wallet_hotkey, netuid, network, rpc_url) -> bool:
    import bittensor as bt

    wallet = bt.Wallet(
        name=wallet_name or _load_config_value('wallet') or 'default',
        hotkey=wallet_hotkey or _load_config_value('hotkey') or 'default',
    )
    try:
        subtensor = bt.Subtensor(network=_resolve_endpoint(network, rpc_url))
        response = subtensor.set_commitment(wallet=wallet, netuid=netuid, data=commitment)
    except Exception as e:
        raise click.ClickException(f'commit failed: {e}') from e
    if not getattr(response, 'success', response):
        raise click.ClickException(f'commitment rejected: {getattr(response, "message", response)}')
    return True


def register_challenge_commands(cli):
    """Register `gitt challenge` with the root CLI group."""
    cli.add_command(challenge_group, name='challenge')
