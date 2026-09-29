# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``gitt challenge`` through the root CLI. eval: a copy of the king gains nothing, the same inputs write the same
bytes, the hashed snapshot is what runs, and without a sandbox nothing runs. init scaffolds from the KING; attest
signs only a crown; submit needs --agree-cla."""

import json
import shutil

from click.testing import CliRunner

from gittensor.challenges import runner
from gittensor.challenges.attestation import Attestation, verify
from gittensor.challenges.checkout import ATTESTATION
from gittensor.challenges.head_to_head import solver_sha
from gittensor.cli.main import cli
from tests.challenges.conftest import FAKE_MODULE, requires_sandbox


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


def test_init_scaffolds_the_next_solver_from_the_king_and_prints_the_problem(challenge_repo, tmp_path, monkeypatch):
    upstream = challenge_repo()
    (work := tmp_path / 'work').mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr('gittensor.challenges.cli.fork_and_clone', lambda repo, dest: shutil.copytree(upstream, dest))

    first = CliRunner().invoke(cli, ['challenge', 'init', 'someone/gt-challenge-fake', '--login', 'alice'])
    CliRunner().invoke(cli, ['challenge', 'init', 'someone/gt-challenge-fake', '--login', 'alice'])

    assert first.exit_code == 0, first.output
    assert 'Echo the number back.' in first.output and 'KING: baselines/good' in first.output
    mine = work / 'gt-challenge-fake' / 'solvers' / 'alice'
    assert sorted(p.name for p in mine.iterdir()) == ['1', '2']
    assert solver_sha(mine / '1') == solver_sha(upstream / 'baselines' / 'good')


def attest(root, monkeypatch, challenger):
    shutil.copytree(challenger, root / 'solvers' / 'alice' / '1')
    monkeypatch.chdir(root)
    monkeypatch.setattr('gittensor.challenges.cli.finalized_block', lambda network: (1000, 'ab' * 32))
    return CliRunner().invoke(cli, ['challenge', 'attest', '--login', 'alice', '--dev-key', str(root.parent / 'k')])


@requires_sandbox
def test_attest_refuses_a_non_crown_and_writes_nothing(challenge_repo, solver, monkeypatch):
    root = challenge_repo('good')

    result = attest(root, monkeypatch, solver('crash'))

    assert result.exit_code != 0 and 'no crown (3 of 3 seeds are invalid)' in result.output
    assert not (root / ATTESTATION).exists()


@requires_sandbox
def test_attest_signs_a_crown_on_the_finalized_block(challenge_repo, solver, monkeypatch):
    root = challenge_repo('crash')

    result = attest(root, monkeypatch, solver('good'))

    assert result.exit_code == 0, result.output
    att = Attestation.from_json((root / ATTESTATION).read_text())
    assert verify(att, att.signer['pubkey']) and (att.seed_block, att.result['seed_block_hash']) == (1000, 'ab' * 32)
    assert att.result['crown'] and att.result['king']['sha'] == solver_sha(root / 'baselines' / 'crash')


def test_submit_without_agreeing_to_the_cla_refuses(challenge_repo, monkeypatch):
    monkeypatch.chdir(challenge_repo())

    result = CliRunner().invoke(cli, ['challenge', 'submit', '--login', 'alice'])

    assert result.exit_code != 0 and 'CLA.md and LICENSING.md' in result.output and '--agree-cla' in result.output
