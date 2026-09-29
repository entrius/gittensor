# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``gitt challenge`` through the root CLI. eval: a copy of the king gains nothing, the same inputs write the same
bytes, the hashed snapshot is what runs, and without a sandbox nothing runs. init scaffolds from the KING, once per
solver not yet on main; attest signs only a crown, with a signer upstream accepts; submit needs --agree-cla and pushes
exactly one commit on upstream main."""

import json
import os
import shutil
import subprocess

import pytest
from click.testing import CliRunner

from gittensor.challenges import runner
from gittensor.challenges.attestation import Attestation, dev_pubkey, sign_dev, verify
from gittensor.challenges.checkout import ATTESTATION, CONFIG
from gittensor.challenges.head_to_head import solver_sha
from gittensor.cli.main import cli
from tests.challenges.conftest import CHALLENGE_JSON, FAKE_MODULE, requires_sandbox


def eval_args(challenger, king, json_path):
    return [
        *['challenge', 'eval', FAKE_MODULE, str(challenger), '--king', str(king), '--tier', 'small'],
        *['--seeds', '3', '--seed-block-hash', '0x' + 'AB' * 32, '--json', str(json_path)],
    ]


@requires_sandbox
def test_a_copy_of_the_king_gains_nothing_and_the_same_inputs_write_the_same_bytes(solver, tmp_path):
    king = solver('good')
    shutil.copytree(king, tmp_path / 'copy')

    first = CliRunner().invoke(cli, eval_args(tmp_path / 'copy', king, tmp_path / 'a.json'))
    CliRunner().invoke(cli, eval_args(tmp_path / 'copy', king, tmp_path / 'b.json'))

    assert first.exit_code == 0, first.output
    assert (tmp_path / 'a.json').read_bytes() == (tmp_path / 'b.json').read_bytes()
    doc = json.loads((tmp_path / 'a.json').read_text())
    assert (doc['seed_block_hash'], doc['n'], doc['challenger']['valid']) == ('ab' * 32, 3, 3)
    assert doc['king']['sha'] == doc['challenger']['sha'] and doc['king']['scores'] == [1.0, 1.0, 1.0]
    assert (doc['mean_gain'], doc['lower_99'], doc['crown']) == (0.0, 0.0, False)


@requires_sandbox
def test_the_code_hashed_is_the_code_that_ran_even_if_the_directory_changes_meanwhile(solver, tmp_path, monkeypatch):
    challenger, evaluate = solver('good'), runner.evaluate
    sha = solver_sha(challenger)

    def swap_then_evaluate(*args):
        (challenger / 'solve').write_text('#!/bin/sh\nexit 3\n')
        return evaluate(*args)

    monkeypatch.setattr(runner, 'evaluate', swap_then_evaluate)
    CliRunner().invoke(cli, eval_args(challenger, solver('evaluator-python'), tmp_path / 'out.json'))

    doc = json.loads((tmp_path / 'out.json').read_text())
    assert (doc['challenger']['sha'], doc['challenger']['valid']) == (sha, 3)


def test_without_a_sandbox_nothing_runs(solver, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'sandbox_error', lambda: 'bwrap is not installed')

    result = CliRunner().invoke(cli, eval_args(solver('good'), solver('good'), tmp_path / 'out.json'))

    assert result.exit_code != 0 and 'no sandbox here' in result.output and not (tmp_path / 'out.json').exists()


CLA_LINE = (
    '- [x] I agree to the Contributor License Agreement in CLA.md and that this solver is licensed under LICENSING.md.'
)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    for who in ('AUTHOR', 'COMMITTER'):
        monkeypatch.setenv(f'GIT_{who}_NAME', 'miner')
        monkeypatch.setenv(f'GIT_{who}_EMAIL', 'miner@example.com')


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def publish(repo):
    """``repo`` as a bare upstream at ``.../github.com/o/<name>.git``."""
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'init')
    bare = repo.parent / 'github.com' / 'o' / f'{repo.name}.git'
    git(repo.parent, 'clone', '-q', '--bare', str(repo), str(bare))
    return bare


def clone(bare, dest):
    """What ``gh repo fork --clone`` leaves: the challenge repo as ``upstream``, a bare fork as ``origin``."""
    git(bare.parent, 'clone', '-q', '--bare', str(bare), str(dest.parent / 'fork.git'))
    git(bare.parent, 'clone', '-q', '-o', 'upstream', str(bare), str(dest))
    git(dest, 'remote', 'add', 'origin', str(dest.parent / 'fork.git'))


def checkout(challenge_repo, tmp_path, king='good', key=None):
    repo = challenge_repo(king)
    if key:
        config = json.loads((repo / CONFIG).read_text())
        (repo / CONFIG).write_text(json.dumps({**config, 'dev_attestation_pubkey': dev_pubkey(key)}))
    (work := tmp_path / 'work').mkdir()
    clone(publish(repo), work / repo.name)
    return work / repo.name


def test_init_scaffolds_from_the_king_once_per_solver_not_yet_on_main(challenge_repo, tmp_path, monkeypatch):
    bare = publish(challenge_repo())
    (work := tmp_path / 'work').mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr('gittensor.challenges.cli.fork_and_clone', lambda repo, dest: clone(bare, dest))
    root, mine = work / 'gt-challenge-fake', work / 'gt-challenge-fake' / 'solvers' / 'alice'

    def init():
        return CliRunner().invoke(cli, ['challenge', 'init', 'o/gt-challenge-fake', '--login', 'alice'])

    first, again = init(), init()
    assert first.exit_code == 0, first.output
    assert 'Echo the number back.' in first.output and 'KING: baselines/good' in first.output
    assert [p.name for p in mine.iterdir()] == ['1'] and 'not on main yet' in again.output

    git(root, 'add', 'solvers')
    git(root, 'commit', '-qm', 'crown')
    git(root, 'push', '-q', 'upstream', 'HEAD:main')
    init()

    assert sorted(p.name for p in mine.iterdir()) == ['1', '2']
    assert solver_sha(mine / '2') == solver_sha(root / 'baselines' / 'good')


def attest(root, monkeypatch, challenger, key):
    shutil.copytree(challenger, root / 'solvers' / 'alice' / '1')
    monkeypatch.chdir(root)
    monkeypatch.setattr('gittensor.challenges.cli.finalized_block', lambda network: (1000, 'ab' * 32))
    return CliRunner().invoke(cli, ['challenge', 'attest', '--login', 'alice', '--dev-key', str(key)])


def test_attest_refuses_a_signer_upstream_does_not_accept_before_running(challenge_repo, solver, tmp_path, monkeypatch):
    root = checkout(challenge_repo, tmp_path, key=tmp_path / 'accepted.key')
    monkeypatch.setattr(runner, 'evaluate', lambda *args: pytest.fail('ran'))

    result = attest(root, monkeypatch, solver('good'), tmp_path / 'other.key')

    assert result.exit_code != 0 and 'does not accept dev signer' in result.output


@requires_sandbox
def test_attest_refuses_a_non_crown_and_writes_nothing(challenge_repo, solver, tmp_path, monkeypatch):
    root = checkout(challenge_repo, tmp_path, 'good', key := tmp_path / 'dev.key')

    result = attest(root, monkeypatch, solver('crash'), key)

    assert result.exit_code != 0 and 'no crown (3 of 3 seeds are invalid)' in result.output
    assert not (root / ATTESTATION).exists()


@requires_sandbox
def test_attest_signs_a_crown_on_the_finalized_block(challenge_repo, solver, tmp_path, monkeypatch):
    root = checkout(challenge_repo, tmp_path, 'crash', key := tmp_path / 'dev.key')

    result = attest(root, monkeypatch, solver('good'), key)

    assert result.exit_code == 0, result.output
    att = Attestation.from_json((root / ATTESTATION).read_text())
    assert verify(att, att.signer['pubkey']) and (att.seed_block, att.result['seed_block_hash']) == (1000, 'ab' * 32)
    assert att.result['crown'] and att.result['king']['sha'] == solver_sha(root / 'baselines' / 'crash')


def test_submit_without_agreeing_to_the_cla_refuses(challenge_repo, monkeypatch):
    monkeypatch.chdir(challenge_repo())

    result = CliRunner().invoke(cli, ['challenge', 'submit', '--login', 'alice'])

    assert result.exit_code != 0 and 'CLA.md and LICENSING.md' in result.output and '--agree-cla' in result.output


def test_submit_pushes_one_commit_on_upstream_main_and_opens_the_pr(challenge_repo, tmp_path, monkeypatch):
    root = checkout(challenge_repo, tmp_path, 'good', key := tmp_path / 'dev.key')
    shutil.copytree(root / 'baselines' / 'good', solver := root / 'solvers' / 'alice' / '1')
    result = {
        **{k: CHALLENGE_JSON[k] for k in ('challenge_id', 'module', 'tier', 'margin')},
        **{'n': CHALLENGE_JSON['seeds'], 'seed_block_hash': 'ab' * 32, 'mean_gain': 0.05, 'crown': True},
        **{'challenger': {'sha': solver_sha(solver)}, 'king': {'sha': solver_sha(root / 'baselines' / 'good')}},
    }
    (root / ATTESTATION).write_text(sign_dev(key, result, 1000).to_json())
    (gh := tmp_path / 'bin' / 'gh').parent.mkdir()
    gh.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> {tmp_path}/gh.log\n[ "$1" = pr ] && echo https://pr/1\nexit 0\n')
    gh.chmod(0o755)
    monkeypatch.setenv('PATH', f'{gh.parent}:{os.environ["PATH"]}')
    monkeypatch.setattr('gittensor.challenges.cli.chain_now', lambda network, block: (1010, 'ab' * 32))
    monkeypatch.chdir(root)

    out = CliRunner().invoke(cli, ['challenge', 'submit', '--agree-cla', '--login', 'alice'])

    assert out.exit_code == 0, out.output
    fork, upstream = tmp_path / 'work' / 'fork.git', tmp_path / 'github.com' / 'o' / 'gt-challenge-fake.git'
    commit = git(fork, 'rev-parse', 'challenge/alice-1')
    assert git(fork, 'rev-parse', f'{commit}^@') == git(upstream, 'rev-parse', 'main')
    assert git(fork, 'diff-tree', '--no-commit-id', '--name-only', '-r', commit).split() == [
        'attestation.json',
        'solvers/alice/1/solve',
    ]
    gh_args = (tmp_path / 'gh.log').read_text().splitlines()
    assert 'https://pr/1' in out.output and 'o/gt-challenge-fake' in gh_args and CLA_LINE in gh_args
