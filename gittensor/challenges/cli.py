# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``gitt challenge``: the optimization challenges.

gitt challenge init CHALLENGE                   fork and clone the repo, scaffold solvers/<login>/<n>/
gitt challenge eval [CHALLENGE]                 in a checkout: your newest solver vs KING on the latest finalized block
gitt challenge eval <module> <challenger_dir> --king DIR --seed-block-hash HEX [--json PATH]
gitt challenge attest [CHALLENGE]               the official run: writes attestation.json when it is a crown
gitt challenge submit [CHALLENGE] --agree-cla   checks attestation.json, opens the one-commit PR
"""

from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import click
from rich.markup import escape
from rich.table import Table

from gittensor.challenges import runner
from gittensor.challenges.attestation import Attestation, sign_dev
from gittensor.challenges.checkout import ATTESTATION, CONFIG, Checkout, repo_name, submission_error
from gittensor.challenges.head_to_head import Entry, canonical, report, snapshot, solver_sha
from gittensor.cli.help import StyledGroup
from gittensor.cli.helpers import console, err_console

SOLVER_DIR = click.Path(exists=True, file_okay=False, path_type=Path)
DEV_KEY = Path('~/.gittensor/challenge-dev.key')
MAIN = 'main'
CLA_LINE = (
    '- [x] I agree to the Contributor License Agreement in CLA.md and that this solver is licensed under LICENSING.md.'
)
CHALLENGE_ARG = click.argument('challenge', required=False)
LOGIN_OPTION = click.option('--login', help='Your GitHub login (default: `gh api user`).')
NETWORK_OPTION = click.option('--network', default='finney', show_default=True, help='Chain for the seed block.')


@click.group(name='challenge', cls=StyledGroup)
def challenge_group():
    """Optimization challenges: run a solver head to head against the king (the current crown)."""


def finalized_block(network: str) -> tuple[int, str]:
    """The chain's latest finalized block: its number and its hash (64 lowercase hex)."""
    import bittensor as bt

    substrate = bt.Subtensor(network=network).substrate
    block_hash = substrate.get_chain_finalised_head()
    return substrate.get_block_number(block_hash), block_hash.lower().removeprefix('0x')


def run(*cmd: str, cwd: Path | None = None, env: dict | None = None) -> str:
    try:
        done = subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise click.ClickException(f'{cmd[0]} is not installed') from e
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f'`{" ".join(cmd)}` failed: {(e.stderr or e.stdout).strip()}') from e
    return done.stdout.strip()


def github_login(login: str | None) -> str:
    try:
        return login or run('gh', 'api', 'user', '--jq', '.login')
    except click.ClickException as e:
        raise click.ClickException(f'cannot tell your GitHub login ({e.message}): pass --login') from e


def fork_and_clone(repo: str, dest: Path) -> None:
    """Your fork as ``origin`` and the challenge repo as ``upstream``; without ``gh``, a plain clone."""
    if shutil.which('gh'):
        run('gh', 'repo', 'fork', repo, '--clone', '--default-branch-only', cwd=dest.parent)
    else:
        run('git', 'clone', f'https://github.com/{repo}.git', str(dest))


def require_checkout(challenge: str | None) -> Checkout:
    if checkout := Checkout.find(challenge):
        return checkout
    raise click.ClickException(f'not in a challenge checkout (no {CONFIG}): run `gitt challenge init` first')


def newest_solver(checkout: Checkout, login: str) -> Path:
    if dirs := checkout.solver_dirs(login):
        return dirs[-1]
    raise click.ClickException(f'no solvers/{login}/<n>/ in {checkout.root}: run `gitt challenge init` first')


def parse_hash(ctx, param, value: str | None) -> str | None:
    if value is None:
        return None
    value = value.lower().removeprefix('0x')
    if not re.fullmatch('[0-9a-f]{64}', value):
        raise click.BadParameter(f'{value!r} is not a 32-byte hex block hash', ctx, param)
    return value


@challenge_group.command('init')
@click.argument('challenge')
@LOGIN_OPTION
def init_command(challenge, login):
    """Fork and clone CHALLENGE (`intents` or OWNER/NAME) and scaffold your next solver from the KING's."""
    repo = repo_name(challenge)
    dest = Path.cwd() / repo.split('/')[1]
    if not (dest / CONFIG).is_file():
        fork_and_clone(repo, dest)
    if not (dest / CONFIG).is_file():
        raise click.ClickException(f'{repo} has no {CONFIG}: not a challenge repo')
    checkout = Checkout.load(dest)
    solver = checkout.next_solver_dir(github_login(login))
    snapshot(checkout.root / checkout.king, solver)

    config = checkout.config
    console.print(f'[bold]{config["challenge_id"]}[/bold] in {dest}\n\n{escape(checkout.summary())}\n')
    console.print(f'KING: {checkout.king}  ·  tier {config["tier"]}: {tier_limits(config)}  ·  {config["seeds"]} seeds')
    console.print(f'your solver: {solver.relative_to(Path.cwd())} (a copy of the KING to start from)\n')
    console.print(
        f'next: cd {dest.name}, edit {solver.relative_to(dest)}, then\n'
        '  gitt challenge eval     a free local head to head against KING\n'
        '  gitt challenge attest   on the Polaris VM: the official run, writes attestation.json\n'
        '  gitt challenge submit   checks it and opens the one-commit PR'
    )


def tier_limits(config: dict) -> str:
    try:
        tier = importlib.import_module(config['module']).TIERS[config['tier']]
    except ImportError:
        return f'install {config.get("package") or config["module"]} to see its limits'
    return f'{tier.time_limit_s:g}s and {tier.memory_mb} MB per instance'


@challenge_group.command('eval')
@click.argument('module', required=False)
@click.argument('challenger_dir', type=SOLVER_DIR, required=False)
@click.option('--king', 'king_dir', type=SOLVER_DIR, help='The solver holding the crown (default: KING).')
@click.option('--tier', help='Challenge tier (its size, time and memory limits) [default: standard].')
@click.option('--seeds', type=click.IntRange(1, 10_000), metavar='N', help='Instances [default: 1000].')
@click.option('--seed-block-hash', callback=parse_hash, help='Block hash the seeds derive from (64 hex).')
@click.option('--margin', type=float, help='Gain the 99% lower bound must reach [default: 0.01].')
@LOGIN_OPTION
@NETWORK_OPTION
@click.option('--json', 'json_path', type=click.Path(dir_okay=False, path_type=Path), help='Write the canonical JSON.')
def eval_command(module, challenger_dir, king_dir, tier, seeds, seed_block_hash, margin, login, network, json_path):
    """Run CHALLENGER_DIR (an executable `solve`) head to head against the --king solver on challenge MODULE.

    [dim]In a challenge checkout, with no CHALLENGER_DIR: your newest solvers/<login>/<n>/ against KING, with
    challenge.json's tier, seeds and margin, on the latest finalized block (options override); MODULE is then
    optional and names the checkout ./gt-challenge-<MODULE>.

    MODULE is the package's import name (gt_challenge_intents). Seed i is sha256('<hash>:i'); each instance is
    generated once and both solvers run `./solve <instance_dir> <output_dir>` on it in turn, sandboxed under the
    tier's limits. A timeout, crash or invalid output scores 0. The challenger takes the crown when every one of its
    seeds is valid and the 99% lower bound of its mean gain over the king is at least the margin.[/dim]
    """
    if challenger_dir:
        if not (king_dir and seed_block_hash):
            raise click.UsageError('with CHALLENGER_DIR, pass --king and --seed-block-hash')
        config = {'tier': 'standard', 'seeds': 1000, 'margin': 0.01}
    else:
        checkout = require_checkout(module)
        config = checkout.config
        module, challenger_dir = config['module'], newest_solver(checkout, github_login(login))
        king_dir = king_dir or checkout.root / checkout.king
        seed_block_hash = seed_block_hash or finalized_block(network)[1]
    tier, seeds = tier or config['tier'], seeds or config['seeds']
    margin = config['margin'] if margin is None else margin
    doc = head_to_head(module, challenger_dir, king_dir, tier, seeds, seed_block_hash, margin)
    if json_path:
        json_path.write_text(canonical(doc))


def head_to_head(
    module: str, challenger_dir: Path, king_dir: Path, tier: str, seeds: int, seed_block_hash: str, margin: float
) -> dict:
    """Run the pair, print the table, return the report."""
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
        canonical(doc)
    except ValueError as e:  # a non-finite mean, gain or bound: nothing is written
        raise click.ClickException(f'cannot report: {e}') from e

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
    return doc


@challenge_group.command('attest')
@CHALLENGE_ARG
@click.option('--dev-key', type=Path, default=DEV_KEY, show_default=True, help='ed25519 key file (made on first use).')
@click.option(
    '--image', envvar='GT_CHALLENGE_IMAGE', help='The evaluator image this runs in [env: GT_CHALLENGE_IMAGE].'
)
@LOGIN_OPTION
@NETWORK_OPTION
def attest_command(challenge, dev_key, image, login, network):
    """The official run: your newest solver vs KING with challenge.json's settings on the latest finalized block.

    [dim]Run it in the checkout on the Polaris VM. Writes a signed attestation.json only when the result is a crown,
    so a loser is never submitted. Signed with a dev key for now; Polaris signing is not wired yet.[/dim]
    """
    checkout = require_checkout(challenge)
    config, solver = checkout.config, newest_solver(checkout, github_login(login))
    block, block_hash = finalized_block(network)
    console.print(f'{solver.relative_to(checkout.root)} vs KING {checkout.king} on block {block}')
    king = checkout.root / checkout.king
    doc = head_to_head(config['module'], solver, king, config['tier'], config['seeds'], block_hash, config['margin'])
    if not doc['crown']:
        invalid = doc['n'] - doc['challenger']['valid']
        why = f'{invalid} of {doc["n"]} seeds are invalid' if invalid else 'the 99% lower bound is under the margin'
        raise click.ClickException(f'no crown ({why}): nothing written, so nothing to submit')
    att = sign_dev(dev_key, doc, block, image)
    (checkout.root / ATTESTATION).write_text(att.to_json())
    console.print(f'wrote {ATTESTATION} (dev signer {att.signer["pubkey"]}); next: gitt challenge submit --agree-cla')
    if att.signer['pubkey'] != config.get('dev_attestation_pubkey'):
        err_console.print('[yellow]challenge.json does not accept this dev signer: submit will refuse it[/yellow]')


@challenge_group.command('submit')
@CHALLENGE_ARG
@click.option('--agree-cla', is_flag=True, help="You agree to the repo's CLA.md and license under LICENSING.md.")
@LOGIN_OPTION
@NETWORK_OPTION
def submit_command(challenge, agree_cla, login, network):
    """Check attestation.json as the maintainer will, then open the one-commit PR: your solver + attestation.json.

    [dim]Refuses a signature that does not verify, a non-crown, a run that does not match challenge.json, a solver
    other than the one attested, a KING that has moved and a seed block older than freshness_blocks.[/dim]
    """
    if not agree_cla:
        raise click.ClickException(
            'read CLA.md and LICENSING.md in the challenge repo, then pass --agree-cla to agree and license your solver'
        )
    checkout = require_checkout(challenge)
    login = github_login(login)
    solver, root = newest_solver(checkout, login), checkout.root
    try:
        att = Attestation.from_json((root / ATTESTATION).read_text())
    except (OSError, ValueError) as e:
        raise click.ClickException(f'cannot read {ATTESTATION} ({e}): run `gitt challenge attest` first') from e

    def git(*args: str, env: dict | None = None) -> str:
        return run('git', *args, cwd=root, env=env)

    if 'upstream' not in git('remote').split():
        raise click.ClickException('no `upstream` remote: clone with `gitt challenge init` so origin is your fork')
    if not (match := re.search(r'github\.com[:/](.+?)(\.git)?/?$', git('remote', 'get-url', 'upstream'))):
        raise click.ClickException('the `upstream` remote is not a GitHub repo')
    repo = match[1]
    git('fetch', '--quiet', 'upstream', MAIN)
    base, path = f'upstream/{MAIN}', solver.relative_to(root).as_posix()
    king = git('show', f'{base}:KING')
    king_sha = git('rev-parse', f'{base}:{king}')
    if subprocess.run(['git', 'cat-file', '-e', f'{base}:{path}'], cwd=root, capture_output=True).returncode == 0:
        raise click.ClickException(f'{path} is already on {MAIN}: scaffold a new one with `gitt challenge init`')
    with tempfile.TemporaryDirectory() as tmp:  # a private index: the working tree and its index are untouched
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(tmp, 'index'))}
        git('read-tree', base, env=env)
        git('add', '--', path, ATTESTATION, env=env)
        tree = git('write-tree', env=env)
    block = finalized_block(network)[0]
    if error := submission_error(att, checkout.config, git('rev-parse', f'{tree}:{path}'), king_sha, block):
        raise click.ClickException(f'not submitted: {error}')

    result, n = att.result, solver.name
    gain = 'the king scored 0' if result['mean_gain'] is None else f'gain {result["mean_gain"]:+.2%}'
    title = f'{result["challenge_id"]}: {login}/{n}, {gain}'
    commit = git('commit-tree', tree, '-p', base, '-m', title)
    branch = f'challenge/{login}-{n}'
    git('push', '--quiet', 'origin', f'{commit}:refs/heads/{branch}')
    body = f'{path} against KING {king} on seed block {att.seed_block}.\n\n{CLA_LINE}\n'
    head = f'{login}:{branch}'
    url = run('gh', 'pr', 'create', '--repo', repo, '--base', MAIN, '--head', head, '--title', title, '--body', body)
    console.print(f'opened {url}\nno pushes to {branch} from now on: a push closes the PR')


def register_challenge_commands(cli):
    """Register `gitt challenge` with the root CLI group."""
    cli.add_command(challenge_group, name='challenge')
