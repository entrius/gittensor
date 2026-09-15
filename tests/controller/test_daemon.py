# The MIT License (MIT)
# Copyright © 2025 Entrius

"""gitt controller run over fakes: a slow start on one box blocks neither the proof round on another box nor the other
loops; run owns the state directory (one-shots and a second run refuse beside it); run serves its loops, stops on
SIGTERM with state written, and status reads what it did."""

import json
import os
import shutil
import signal
import threading
import time
from unittest.mock import patch

import pytest

import gittensor.cli.main  # noqa: F401  (the CLI package must load before gittensor.controller.cli: circular import)
from gittensor.controller import cli as ctl
from gittensor.controller.checks.state import IDLE, LEASED, STARTING, BoxState
from gittensor.controller.daemon import Controller, Intervals
from tests.controller.conftest import AGENT_DIGEST, FIXTURES, NETWORK_TARGETS, UUID_5090_B, FakeProof
from tests.controller.test_cli import FAKE_PROOF, HK_A, HK_B, NET, admit, box_runner, invoke, round_args
from tests.controller.test_placement import FakeDocker, idle_box, keypair, make_world, seed


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


@pytest.fixture(autouse=True)
def _wide_terminal(monkeypatch):
    monkeypatch.setenv('COLUMNS', '250')


@pytest.fixture
def state(tmp_path):
    root = tmp_path / 'state'
    root.mkdir()
    (root / 'gt_ca').write_text('placeholder: the runner is faked\n')
    shutil.copy(FIXTURES / 'nvml_allowlist.json', root / 'nvml_allowlist.json')
    return root


def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'timed out'
        time.sleep(0.01)


def test_a_slow_start_on_one_box_blocks_neither_the_round_on_another_nor_the_other_loops(world):
    root, registry = world
    shutil.copy(FIXTURES / 'nvml_allowlist.json', root / 'nvml_allowlist.json')
    seed(root, idle_box('hkA', uuids=(UUID_5090_B,)), BoxState(HK_B, host='10.0.0.2', port=2200), replicas=1)
    loading = threading.Event()  # the "600 s" model load on box A: docker run does not return until it is set
    slow = FakeDocker(gpus=(UUID_5090_B,), hold=loading)
    setup = ctl._setup(
        root, None, FAKE_PROOF, (), (AGENT_DIGEST,), (), 'entrius/gt-proof:test', None, NETWORK_TARGETS, 100
    )
    controller = Controller(
        ctl.StateDir(root),
        registry,
        make_runner=lambda box, purpose: slow.runner,
        run_round=lambda proof, **shared: ctl.run_round(setup, proof, lock_wait_s=0.2, **shared),
        load_proof=FakeProof,
        intervals=Intervals(),
    )
    try:
        assert controller.reconcile_once().launched == ['hkA']
        _until(lambda: controller.box_locks.held('hkA') and slow.commands('docker run -d'))
        assert controller.boxes.boxes['hkA'].cards[UUID_5090_B].state == STARTING

        with patch.object(
            ctl, '_make_runner', side_effect=lambda st, box, ca, purpose: {HK_B: box_runner()}[box.box_id]
        ):
            began = time.monotonic()
            report = controller.round_once()
            took = time.monotonic() - began
        rows = {r.box.box_id: r for r in report.boxes}
        assert took < 5 and rows[HK_B].verdict.admitted and controller.boxes.boxes[HK_B].status == IDLE
        assert 'box busy' in rows['hkA'].busy and rows['hkA'].verdict is None and rows['hkA'].runner is None

        again = controller.reconcile_once()
        assert again.in_flight == ['hkA'] and again.actions == [] and again.launched == []
        assert controller.watch_once().visited == []
        assert controller.boxes.boxes['hkA'].cards[UUID_5090_B].state == STARTING  # the round did not touch it
    finally:
        loading.set()
    assert controller.reconciler.join(5)
    assert controller.boxes.boxes['hkA'].cards[UUID_5090_B].state == LEASED
    status = json.loads((root / 'controller.json').read_text())
    assert (
        status['round']['boxes'][HK_B]['verdict'] == 'ADMIT' and 'box busy' in status['round']['boxes']['hkA']['busy']
    )
    assert status['reconcile']['last_background']['actions'][0]['states'] == [IDLE, STARTING, LEASED]


def test_run_owns_the_state_directory(state, tmp_path):
    keypair(tmp_path)
    pub = tmp_path / 'release.pub'
    admit(state)
    with ctl.StateDir(state).run_lock():
        one_shots = (
            round_args(state),
            ['reconcile', '--state-dir', state, '--release-pubkey', pub],
            ['check', HK_A, '--state-dir', state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST, *NET],
        )
        for args in one_shots:
            result = invoke(*args)
            assert result.exit_code == 2 and 'controller running' in result.output, result.output
            assert 'gitt controller status' in result.output
        second = invoke('run', '--state-dir', state, '--proof', FAKE_PROOF, '--release-pubkey', pub)
        assert second.exit_code == 2 and 'controller running' in second.output
        assert json.loads(invoke('status', '--state-dir', state, '--json').stdout)['running'] is True
    assert json.loads(invoke('status', '--state-dir', state, '--json').stdout)['running'] is False
    with patch.object(ctl, '_make_runner', side_effect=lambda st, box, ca, purpose: box_runner()):
        assert invoke(*round_args(state)).exit_code == 0  # free again


def test_run_serves_its_loops_stops_on_sigterm_and_status_reads_what_it_did(state, tmp_path):
    keypair(tmp_path)
    admit(state)
    status_file = state / 'controller.json'

    def sigterm_once_running():
        _until(lambda: status_file.exists() and json.loads(status_file.read_text()).get('round', {}).get('n'), 10)
        os.kill(os.getpid(), signal.SIGTERM)

    killer = threading.Thread(target=sigterm_once_running, daemon=True)
    with patch.object(ctl, '_make_runner', side_effect=lambda st, box, ca, purpose: box_runner()):
        killer.start()
        result = invoke(
            'run', '--state-dir', state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST, *NET,
            '--release-pubkey', tmp_path / 'release.pub', '--round-interval', 3600, '--reconcile-interval', 0.1,
            '--max-seconds', 30,
        )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert 'round 1' in result.output and f'{HK_A} ADMIT→IDLE' in result.output and 'stopping' in result.output
    status = json.loads(status_file.read_text())
    assert status['round']['n'] == 1 and status['round']['exit_code'] == 0 and status['stopped_at']
    assert status['reconcile']['n'] >= 1 and signal.getsignal(signal.SIGTERM) is not None

    shown = invoke('status', '--state-dir', state)
    assert shown.exit_code == 0, shown.output
    assert 'not running' in shown.output and 'last round 1' in shown.output and 'IDLE' in shown.output
    payload = json.loads(invoke('status', '--state-dir', state, '--json').stdout)
    assert payload['running'] is False and payload['controller']['round']['n'] == 1
    (box,) = payload['boxes']
    assert box['hotkey'] == HK_A and box['status'] == IDLE and [c['state'] for c in box['cards']] == [IDLE]
