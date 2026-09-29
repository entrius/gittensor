# The MIT License (MIT)
# Copyright © 2026 Entrius

"""``gitt challenge eval`` through the root CLI: a copy of the king gains nothing, the same inputs write the same
bytes, and without a sandbox nothing runs."""

import json
import shutil

from click.testing import CliRunner

from gittensor.challenges import runner
from gittensor.cli.main import cli
from tests.challenges.conftest import FAKE_MODULE, requires_sandbox


def eval_args(challenger, king, json_path):
    return [
        *['challenge', 'eval', FAKE_MODULE, str(challenger), '--king', str(king), '--tier', 'small'],
        *['--seeds', '3', '--seed-block-hash', '0x00FF', '--json', str(json_path)],
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
    assert (doc['seed_block_hash'], doc['n'], doc['challenger']['valid']) == ('00ff', 3, 3)
    assert doc['king']['sha'] == doc['challenger']['sha'] and doc['king']['scores'] == [1.0, 1.0, 1.0]
    assert (doc['mean_gain'], doc['lower_99'], doc['crown']) == (0.0, 0.0, False)


def test_without_a_sandbox_nothing_runs(solver, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'sandbox_error', lambda: 'bwrap is not installed')

    result = CliRunner().invoke(cli, eval_args(solver('good'), solver('good'), tmp_path / 'out.json'))

    assert result.exit_code != 0 and 'no sandbox here' in result.output and not (tmp_path / 'out.json').exists()
