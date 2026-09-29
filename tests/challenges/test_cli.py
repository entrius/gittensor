# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``gitt challenge eval``, ``submit`` and ``round`` through the root CLI. The round runs end to end: a bad candidate
costs only itself, and the scorecard it writes is what the validator's ``read_scorecard`` accepts."""

import json
from pathlib import Path

from click.testing import CliRunner

from gittensor.challenges import runner
from gittensor.challenges.attest import verify_dev
from gittensor.cli.main import cli
from gittensor.constants import CHALLENGE_SCORECARD_SCHEMA as SCHEMA
from gittensor.controller.pay.scorecard import read_scorecard
from tests.challenges.conftest import FAKE_MODULE, requires_sandbox


@requires_sandbox
def test_eval_prints_per_seed_and_mean_scores(solver):
    result = CliRunner().invoke(
        cli, ['challenge', 'eval', FAKE_MODULE, str(solver('good')), '--seeds', '2', '--public-seed', 'x', '--json']
    )

    out = json.loads(result.output)
    assert (out['score'], len(out['results']), out['public_seed']) == (1.0, 2, 'x')


def test_submit_without_hippius_writes_the_bundle_to_out(solver, registry_path, tmp_path, monkeypatch):
    monkeypatch.delenv('HIPPIUS_ACCESS_KEY', raising=False)
    args = ['challenge', 'submit', 'fake-echo', str(solver('good')), '--registry', str(registry_path), '--json']

    refused = CliRunner().invoke(cli, args)
    result = CliRunner().invoke(cli, [*args, '--out', str(tmp_path / 'out')])
    creds = {'HIPPIUS_ACCESS_KEY': 'a', 'HIPPIUS_SECRET_KEY': 's', 'HIPPIUS_BUCKET': 'b'}
    uncommitted = CliRunner().invoke(cli, args, env=creds)

    assert refused.exit_code != 0 and 'HIPPIUS' in refused.output
    assert uncommitted.exit_code != 0 and 'needs --commit' in uncommitted.output
    out = json.loads(result.output)
    assert out['commitment'] == f'gt-challenge:fake-echo:{out["sha256"]}' and not out['committed']
    assert (tmp_path / 'out' / f'fake-echo-{out["sha256"]}.tar.gz').is_file()


def test_round_refuses_to_score_without_a_sandbox(solver, registry_path, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'sandbox_error', lambda: 'bwrap is not installed')
    path = tmp_path / 'candidates.json'
    path.write_text(json.dumps([]))
    state = tmp_path / 'state'
    args = [
        '--candidates',
        str(path),
        '--public-seed',
        's',
        '--state-dir',
        str(state),
        '--registry',
        str(registry_path),
    ]

    result = CliRunner().invoke(cli, ['challenge', 'round', *args])

    assert result.exit_code != 0 and 'no sandbox here' in result.output
    assert not (state / 'leaderboard.json').exists() and not (state / 'scorecard').exists()


@requires_sandbox
def test_round_crowns_the_best_and_writes_a_scorecard_the_validator_reads(solver, registry_path, tmp_path):
    entries = json.loads(registry_path.read_text())
    entries['broken'] = {**entries['fake-echo'], 'module': 'no_such_challenge', 'emission_share': 0.25}
    registry_path.write_text(json.dumps(entries))
    candidates = [
        ('broken', 'hk-broken', 80, 'd', solver('good')),
        ('fake-echo', 'hk-good', 100, 'a', solver('good')),
        ('fake-echo', 'hk-garbage', 90, 'b', solver('garbage')),
        ('fake-echo', 'hk-gone', 95, 'e', tmp_path / 'missing'),
        ('not-registered', 'hk-x', 1, 'c', solver('crash')),
    ]
    keys = ('challenge_id', 'hotkey', 'commit_block', 'submission_sha256', 'solver_dir')
    path = tmp_path / 'candidates.json'
    path.write_text(json.dumps([dict(zip(keys, (*c[:3], c[3] * 64, str(c[4])))) for c in candidates]))
    state = tmp_path / 'state'

    result = CliRunner().invoke(
        cli,
        ['challenge', 'round', '--candidates', str(path), '--public-seed', 'block-0xabc', '--state-dir', str(state)]
        + ['--registry', str(registry_path)],
    )

    scorecard, sha = result.output.split()
    issued_at = json.loads(Path(scorecard).read_text())['issued_at']
    doc, read_sha = read_scorecard(scorecard, issued_at, SCHEMA)
    assert read_sha == sha
    assert doc['hotkeys'] == [{'hotkey': 'hk-good', 'weight': 0.5}] and doc['recycle_share'] == 0.5
    broken, echo = doc['challenges']['broken'], doc['challenges']['fake-echo']
    assert broken['king'] is None and [e['score'] for e in broken['evaluated']] == [0.0]
    assert echo['king']['hotkey'] == 'hk-good' and [e['score'] for e in echo['evaluated']] == [1.0, 0.0, 0.0]
    assert json.loads((state / 'leaderboard.json').read_text())['challenges'] == doc['challenges']
    assert verify_dev(doc) and not verify_dev({**doc, 'recycle_share': 0.0})
