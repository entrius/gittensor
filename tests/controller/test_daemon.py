# The MIT License (MIT)
# Copyright © 2025 Entrius

"""gitt controller run over fakes: a slow start on one box blocks neither the proof round on another box nor the other
loops; a drained card and a newly discovered box are proved at the next watch tick; discovery runs before round 1;
run owns the state directory (one-shots and a second run refuse beside it); run serves its loops, stops on SIGTERM
with state written, and status reads what it did."""

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest

import gittensor.cli.main  # noqa: F401  (the CLI package must load before gittensor.controller.cli: circular import)
from gittensor.controller import cli as ctl
from gittensor.controller.checks.runner import regex
from gittensor.controller.checks.state import (
    ADMIT,
    BENCHED,
    CHECKING,
    IDLE,
    LEASED,
    STARTING,
    BoxState,
    StateStore,
)
from gittensor.controller.daemon import Controller, Intervals, SilentReporter
from gittensor.controller.registry import DeploymentStore, Registry
from gittensor.controller.ssh import SshTransportError
from tests.controller.conftest import (
    AGENT_DIGEST,
    FIXTURES,
    NETWORK_TARGETS,
    UUID_5090,
    UUID_5090_B,
    FakeProof,
    challenge_for,
    fixture,
)
from tests.controller.test_cli import FAKE_PROOF, HK_A, HK_B, KEY_2, NET, admit, box_runner, invoke, round_args
from tests.controller.test_discovery import PUB_B, metagraph
from tests.controller.test_placement import ENTRY, FakeDocker, idle_box, keypair, make_world, seed


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


def test_a_drained_card_is_re_proved_at_the_next_watch_tick_and_the_round_does_not_prove_it_twice(world):
    root, registry = world
    shutil.copy(FIXTURES / 'nvml_allowlist.json', root / 'nvml_allowlist.json')
    seed(root, idle_box('hkA'), replicas=1)  # two IDLE cards
    docker = FakeDocker()  # what reconcile and the watch see
    one = fixture('nvidia_smi_5090.csv')
    prover = box_runner(nvidia_smi=one + one.replace(UUID_5090, UUID_5090_B))  # what the proof visits see
    setup = ctl._setup(
        root, None, FAKE_PROOF, (), (AGENT_DIGEST,), (), 'entrius/gt-proof:test', None, NETWORK_TARGETS, 100
    )
    controller = Controller(
        ctl.StateDir(root),
        registry,
        make_runner=lambda box, purpose: docker.runner,
        run_round=lambda proof, **shared: ctl.run_round(setup, proof, **shared),
        load_proof=FakeProof,
        reprove=lambda proof, box_id, **shared: ctl.reprove_box(setup, proof, box_id, **shared),
    )

    def proofs(uuid):
        return [c for c in prover.calls if c.startswith('docker create') and uuid in c]

    def card(uuid):
        return controller.boxes.boxes['hkA'].cards[uuid].state

    with patch.object(ctl, '_make_runner', side_effect=lambda st, box, ca, purpose: prover):
        controller.reconcile_once()
        assert controller.reconciler.join(5)
        (drained,) = [u for u in (UUID_5090, UUID_5090_B) if card(u) == LEASED]
        other = UUID_5090_B if drained == UUID_5090 else UUID_5090
        DeploymentStore(root / 'deployments.json').set(ENTRY, enabled=False)
        controller.reconcile_once()
        assert controller.reconciler.join(5)
        assert card(drained) == CHECKING and not prover.calls  # t = 0: drained, nothing proved yet

        controller.watch_once()  # the next tick
        _until(lambda: card(drained) == IDLE)
        _until(lambda: (controller.status.get('reprove') or {}).get('verdict') == 'ADMIT')
        assert len(proofs(drained)) == 1 and not proofs(other)  # that box, that card only; the IDLE one waits
        assert controller.reprove_once() == [] and len(proofs(drained)) == 1  # later ticks: nothing in CHECKING

        report = controller.round_once()  # t = 20 min: the fleet round, unchanged
        assert report.boxes[0].verdict.admitted
        assert len(proofs(drained)) == 2 and len(proofs(other)) == 1  # proved once by the round, not twice
        controller.watch_once()
        assert len(proofs(drained)) == 2


def test_the_one_box_probe_runs_the_newest_build(world):
    root, registry = world
    shutil.copy(FIXTURES / 'nvml_allowlist.json', root / 'nvml_allowlist.json')
    seed(root, idle_box('hkA'), replicas=1)
    docker = FakeDocker()
    one = fixture('nvidia_smi_5090.csv')
    prover = box_runner(nvidia_smi=one + one.replace(UUID_5090, UUID_5090_B))
    setup = ctl._setup(
        root, None, FAKE_PROOF, (), (AGENT_DIGEST,), (), 'entrius/gt-proof:test', None, NETWORK_TARGETS, 100
    )
    version, probes = ['fake-1'], []

    class Probes(Notes):
        def reprove(self, report):
            probes.append(report.provider)

    controller = Controller(
        ctl.StateDir(root),
        registry,
        make_runner=lambda box, purpose: docker.runner,
        run_round=lambda proof, **shared: ctl.run_round(setup, proof, **shared),
        load_proof=lambda: FakeProof(version[0]),
        reprove=lambda proof, box_id, **shared: ctl.reprove_box(setup, proof, box_id, **shared),
        reporter=Probes(),
    )
    with patch.object(ctl, '_make_runner', side_effect=lambda st, box, ca, purpose: prover):
        assert controller.round_once().provider == 'fake-1'
        controller.reconcile_once()
        assert controller.reconciler.join(5)
        version[0] = 'fake-2'  # `--build-cmd` made a new version between the round and the re-prove
        DeploymentStore(root / 'deployments.json').set(ENTRY, enabled=False)
        controller.reconcile_once()
        assert controller.reconciler.join(5)
        controller.watch_once()
        _until(lambda: probes)
    assert probes == ['fake-2'] and controller.proof is not None and controller.proof.version == 'fake-2'
    staged = [c for c in prover.calls if c.startswith('docker create')]
    assert len(staged) == 3  # two by the round, one by the re-prove
    assert any(f'--challenge {challenge_for(u, "fake-2")}' in staged[-1] for u in (UUID_5090, UUID_5090_B))


def test_a_lease_ended_by_missed_heartbeats_is_re_proved_only_after_the_reconciler_undeployed_it(world):
    root, registry = world
    shutil.copy(FIXTURES / 'nvml_allowlist.json', root / 'nvml_allowlist.json')
    seed(root, idle_box('hkA', uuids=(UUID_5090,)), replicas=1)  # one card
    docker = FakeDocker(gpus=(UUID_5090,))
    prover = box_runner()
    setup = ctl._setup(
        root, None, FAKE_PROOF, (), (AGENT_DIGEST,), (), 'entrius/gt-proof:test', None, NETWORK_TARGETS, 100
    )
    controller = Controller(
        ctl.StateDir(root),
        registry,
        make_runner=lambda box, purpose: docker.runner,
        run_round=lambda proof, **shared: ctl.run_round(setup, proof, **shared),
        load_proof=FakeProof,
        reprove=lambda proof, box_id, **shared: ctl.reprove_box(setup, proof, box_id, **shared),
        intervals=Intervals(heartbeat_s=0),
    )

    def card():
        return controller.boxes.boxes['hkA'].cards[UUID_5090].state

    def proofs():
        return [c for c in prover.calls if c.startswith('docker create')]

    with patch.object(ctl, '_make_runner', side_effect=lambda st, box, ca, purpose: prover):
        controller.reconcile_once()
        assert controller.reconciler.join(5) and card() == LEASED
        (record,) = controller.instances.instances.values()
        docker.runner.on(regex(r'^nvidia-smi --query-gpu'), SshTransportError('10.0.0.1:2200: timed out'))
        for n in (1, 2, 3):
            report = controller.watch_once()
            assert [a.kind for a in report.actions] == ['miss' if n < 3 else 'unreachable']
        assert card() == CHECKING and record.id in controller.instances.instances  # the lease ended, record kept
        assert controller.reprove_once() == [] and not proofs()  # our container may still sit on the card: no proof
        docker.runner.on(regex(r'^nvidia-smi --query-gpu'), docker.respond)  # the box is back
        report = controller.round_once()  # the fleet round skips the card too
        assert report.boxes[0].verdict is None and not proofs()
        assert report.boxes[0].skipped == {UUID_5090: 'CHECKING (instance pending)'} and card() == CHECKING
        controller.reconcile_once()  # the reconciler undeploys it
        assert controller.reconciler.join(5)
        assert record.id not in controller.instances.instances and record.container_id not in docker.containers
        controller.watch_once()  # then the one-box probe re-proves the card
        _until(lambda: card() == IDLE)
        assert len(proofs()) == 1
        controller.reconcile_once()  # and it is placed again
        assert controller.reconciler.join(5) and card() == LEASED
        assert len(docker.commands('docker run -d')) == 2


def test_a_discovered_box_is_proved_at_the_next_watch_tick_and_the_round_does_not_prove_it_twice(world):
    root, registry = world
    shutil.copy(FIXTURES / 'nvml_allowlist.json', root / 'nvml_allowlist.json')
    prover = box_runner()
    setup = ctl._setup(
        root, None, FAKE_PROOF, (), (AGENT_DIGEST,), (), 'entrius/gt-proof:test', None, NETWORK_TARGETS, 100
    )
    controller = Controller(
        ctl.StateDir(root),
        registry,
        make_runner=lambda box, purpose: prover,
        run_round=lambda proof, **shared: ctl.run_round(setup, proof, **shared),
        load_proof=FakeProof,
        reprove=lambda proof, box_id, **shared: ctl.reprove_box(setup, proof, box_id, **shared),
        read_chain=lambda: metagraph((HK_B, PUB_B, 2200, True)),
        scan_host_key=lambda host, port: KEY_2,
    )

    def proofs():
        return [c for c in prover.calls if c.startswith('docker create')]

    with patch.object(ctl, '_make_runner', side_effect=lambda st, box, ca, purpose: prover):
        report = controller.discover_once()  # t = 0, between rounds: the chain publishes a new box
        assert report is not None and [a.kind for a in report.actions] == ['admit']
        assert controller.boxes.boxes[HK_B].status == ADMIT and not proofs()

        assert controller.watch_once().visited == []  # the next tick: its first proof, on its own
        _until(lambda: controller.boxes.boxes[HK_B].status == IDLE)
        _until(lambda: (controller.status.get('reprove') or {}).get('verdict') == 'ADMIT')
        box = controller.boxes.boxes[HK_B]
        assert box.pinned_uuids == [UUID_5090] and box.cards[UUID_5090].state == IDLE and len(proofs()) == 1
        assert controller.status['reprove']['before'] == ADMIT and controller.status['reprove']['after'] == IDLE
        assert controller.reprove_once() == [] and len(proofs()) == 1  # nothing at ADMIT or CHECKING: no more

        report = controller.round_once()  # t = 20 min: the fleet round proves it once more, like any IDLE box
        assert report.boxes[0].verdict.admitted and report.boxes[0].status_before == IDLE and len(proofs()) == 2
        controller.watch_once()
        assert len(proofs()) == 2


def test_discovery_runs_before_round_1_and_without_it_the_round_runs_at_start(tmp_path):
    def controller_for(name, read_chain):
        seen = []
        controller = Controller(
            ctl.StateDir(tmp_path / name).ensure(),
            registry=None,  # type: ignore[arg-type]  (no deployments: the reconciler reads nothing)
            make_runner=lambda box, purpose: None,  # type: ignore[return-value]
            run_round=lambda proof, **shared: (
                seen.append(sorted(shared['store'].boxes)) or ctl.RoundReport('fake', [], [], {})
            ),  # fmt: skip
            load_proof=FakeProof,
            read_chain=read_chain,
            scan_host_key=lambda host, port: KEY_2,
            intervals=Intervals(round_s=3600, reconcile_s=3600, watch_tick_s=3600, scorecard_s=3600, discover_s=3600),
        )
        return controller, seen

    reads = []
    discovering, seen = controller_for('discover', lambda: reads.append(1) or metagraph((HK_B, PUB_B, 2200, True)))
    discovering.start()
    try:
        _until(lambda: seen)
    finally:
        assert discovering.shutdown()
    assert seen == [[HK_B]] and reads == [1]  # round 1 saw the box discovery admitted first, and one read only
    assert discovering.status['discover']['at'] <= discovering.status['round']['started_at']

    plain, seen = controller_for('plain', None)
    plain.start()
    try:
        _until(lambda: seen)
    finally:
        assert plain.shutdown()
    assert seen == [[]] and 'discover' not in plain.status


class Notes(SilentReporter):
    """A reporter that keeps the notes and errors."""

    def __init__(self):
        self.notes, self.errors = [], []

    def note(self, loop, message):
        self.notes.append((loop, message))

    def error(self, loop, message):
        self.errors.append((loop, message))


def _tick_controller(tmp_path, clock, run_round, load_proof: Callable[[], Any] = FakeProof, **kw):
    notes = Notes()
    controller = Controller(
        ctl.StateDir(tmp_path / 'state').ensure(),
        registry=None,  # type: ignore[arg-type]
        make_runner=lambda box, purpose: None,  # type: ignore[return-value]
        run_round=run_round,
        load_proof=load_proof,
        reporter=notes,
        wall=lambda: clock[0],
        intervals=Intervals(round_s=1200),
        **kw,
    )
    return controller, notes


def test_the_round_catches_up_once_after_a_sleep_then_keeps_its_cadence(tmp_path):
    clock, rounds = [0.0], []
    controller, notes = _tick_controller(
        tmp_path, clock, lambda proof, **shared: rounds.append(clock[0]) or ctl.RoundReport('fake', [], [], {})
    )
    for t, expect in ((0, True), (5, False), (600, False), (1200, True), (1800, False)):
        clock[0] = float(t)
        assert controller.round_tick() is expect, t
    assert rounds == [0.0, 1200.0] and not [m for _, m in notes.notes if 'catching up' in m]

    clock[0] = 1200.0 + 90 * 60  # the controller slept 90 min (9/15: 21:51 -> 23:17): one round, at once
    assert controller.round_tick() is True and rounds[-1] == clock[0]
    assert [m for _, m in notes.notes if 'catching up' in m] == [
        'catching up: the last round was 90 min ago (interval 20 min)'
    ]
    for dt, expect in ((5, False), (1199, False), (1200, True), (1205, False)):  # the cadence resumes from it
        clock[0] = 1200.0 + 90 * 60 + dt
        assert controller.round_tick() is expect, dt
    assert len(rounds) == 4 and len([m for _, m in notes.notes if 'catching up' in m]) == 1


def test_a_failed_build_keeps_the_previous_proof_and_is_retried_on_the_next_wake_not_the_next_round(tmp_path):
    clock, version, builds = [0.0], ['fake-1'], []

    def build(command):
        builds.append(command)
        if len(builds) < 3:
            return subprocess.CompletedProcess(command, 100, 'Reading package lists...\n', 'E: apt offline\n')
        version[0] = 'fake-2'
        return subprocess.CompletedProcess(command, 0, 'built fake-2\n', '')

    rounds = []
    controller, notes = _tick_controller(
        tmp_path,
        clock,
        lambda proof, **shared: rounds.append(proof.version) or ctl.RoundReport(proof.version, [], [], {}),
        load_proof=lambda: FakeProof(version[0]),
        build=build,
        build_cmd='ci/gen_secret.sh && ci/build.sh',
    )
    assert controller.round_tick() is True and rounds == ['fake-1']  # tick 1: the round, then the build fails
    assert controller.proof is not None and controller.proof.version == 'fake-1' and len(builds) == 1
    clock[0] = 5.0
    assert controller.round_tick() is False and len(builds) == 2  # tick 2: no round; the build is retried, fails
    assert controller.proof.version == 'fake-1'
    assert [m for _, m in notes.errors] == [
        "build exit 100 in 0 ms: 'E: apt offline'; keeping proof fake-1, retrying in 5 s"
    ] * 2
    clock[0] = 10.0
    assert controller.round_tick() is False and len(builds) == 3  # tick 3: the build makes it: the new version
    assert controller.proof.version == 'fake-2' and rounds == ['fake-1']
    assert notes.notes[-1] == ('round', 'build exit 0 in 0 ms: built fake-2 (recovered)')
    clock[0] = 15.0
    assert controller.round_tick() is False and len(builds) == 3  # no more retries once it passed
    clock[0] = 1200.0
    assert controller.round_tick() is True and rounds == ['fake-1', 'fake-2'] and len(builds) == 4


def test_a_round_that_raises_before_the_first_visit_blames_no_box(tmp_path):
    clock = [0.0]

    def down(proof, **shared):
        raise ConnectionError('allowlist https://example/nvml.json: Network is unreachable')

    controller, notes = _tick_controller(tmp_path, clock, down)
    for hotkey, count in ((HK_A, 1), (HK_B, 0)):
        controller.boxes.put(BoxState(hotkey, status=IDLE, host='10.0.0.1', port=2200, unreachable_count=count))
    assert controller.round_tick() is True and controller.round_once() is None
    assert [b.unreachable_count for b in controller.boxes.boxes.values()] == [1, 0]
    why = 'failed before any box was visited: ConnectionError: allowlist https://example/nvml.json: Network is unreachable'
    assert notes.errors == [('round', f'round {n} {why}') for n in (1, 2)]
    assert 'Network is unreachable' in controller.status['round']['error'] and controller.status['round']['n'] == 2


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


def test_check_force_runs_beside_run_only_on_a_benched_box(state):
    admit(state)
    s = ctl.StateDir(state).store()

    def put(**fields):
        s.put(BoxState.from_dict({**s.get(HK_A).as_dict(), **fields}))

    put(status=BENCHED, bench_until=9e12, bench_count=1, last_failed=['gpu_proof'])
    args = ['check', HK_A, '--state-dir', state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST, *NET]
    with (
        ctl.StateDir(state).run_lock(),
        patch.object(ctl, '_make_runner', side_effect=lambda st, box, ca, purpose: box_runner()),
    ):
        forced = invoke(*args, '--force')
        assert forced.exit_code == 0 and '--force' in forced.output and 'ADMIT' in forced.output, forced.output
        after = ctl.StateDir(state).store().get(HK_A)
        assert after.status == BENCHED and after.bench_until == 9e12 and after.last_failed == ['gpu_proof']

        unforced = invoke(*args)  # without --force: the one-shot refusal, unchanged
        assert unforced.exit_code == 2 and 'controller running' in unforced.output

        for fields in ({'status': ADMIT, 'bench_until': None}, {'bench_until': 1.0}):  # not benched / bench expired
            put(**{'status': BENCHED, **fields})
            refused = invoke(*args, '--force')
            assert refused.exit_code == 2 and 'controller running' in refused.output, refused.output
            assert 'gitt controller status' in refused.output and 'ADMIT' not in refused.output
        assert ctl.StateDir(state).store().get(HK_A).last_check_at is None  # nothing was ever applied


def test_release_beside_run_is_applied_by_the_daemons_next_round(state, tmp_path):
    admit(state)
    s = ctl.StateDir(state).store()
    s.put(
        BoxState.from_dict(
            {**s.get(HK_A).as_dict(), 'status': BENCHED, 'benched_at': 5.0, 'bench_until': 9e12, 'bench_count': 1}
        )
    )
    setup = ctl._setup(
        state, None, FAKE_PROOF, (), (AGENT_DIGEST,), (), 'entrius/gt-proof:test', None, NETWORK_TARGETS, 100
    )
    with ctl.StateDir(state).run_lock():
        controller = Controller(
            ctl.StateDir(state),
            Registry(tmp_path / 'registry'),
            make_runner=lambda box, purpose: box_runner(),
            run_round=lambda proof, **shared: ctl.run_round(setup, proof, **shared),
            load_proof=FakeProof,
        )
        assert controller.boxes.boxes[HK_A].status == BENCHED  # the daemon's in-memory view

        result = invoke('release', HK_A, '--reason', 'false positive', '--state-dir', state, '--json')
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)['pending'] is True
        assert StateStore(state / 'boxes.json').get(HK_A).status == BENCHED  # recorded, not applied: the daemon owns it
        assert 'release requested' in invoke('status', '--state-dir', state).output

        with patch.object(ctl, '_make_runner', side_effect=lambda st, box, ca, purpose: box_runner()):
            report = controller.round_once()
        (row,) = report.boxes
        assert row.status_before == BENCHED and row.verdict.admitted
        after = controller.boxes.boxes[HK_A]
        assert after.status == IDLE and after.standing_events[-1]['kind'] == 'released'
        assert after.standing_events[-1]['reason'] == 'false positive'
        assert StateStore(state / 'boxes.json').get(HK_A).status == IDLE


def test_remove_beside_run_is_applied_by_the_daemons_next_reconcile_pass_once_nothing_runs_there(state, tmp_path):
    from gittensor.controller.reconcile import InstanceRecord
    from gittensor.controller.ssh import pinned_host_key

    admit(state)
    with ctl.StateDir(state).run_lock():
        notes = Notes()
        controller = Controller(
            ctl.StateDir(state),
            Registry(tmp_path / 'registry'),
            make_runner=lambda box, purpose: box_runner(),
            run_round=lambda proof, **shared: None,
            load_proof=FakeProof,
            reporter=notes,
        )
        controller.instances.put(InstanceRecord('i-1', 'e@1', HK_A, UUID_5090, healthy=True))
        assert invoke('remove', HK_A, '--state-dir', state).exit_code == 1  # still carries an instance
        controller.instances.remove('i-1')
        result = invoke('remove', HK_A, '--reason', 'pod returned', '--state-dir', state, '--json')
        assert result.exit_code == 0 and json.loads(result.stdout)['pending'] is True
        assert HK_A in StateStore(state / 'boxes.json').boxes  # recorded, not applied: the daemon owns it
        assert 'removal requested' in invoke('status', '--state-dir', state).output

        with controller.write_lock:
            controller.boxes.merge_from_disk()
            assert controller.remove_requested_boxes() == [HK_A]
        assert HK_A not in controller.boxes.boxes and HK_A not in StateStore(state / 'boxes.json').boxes
        assert not pinned_host_key(state / 'known_hosts', '10.0.0.1', 2200)
        assert any('removed (pod returned)' in m for _, m in notes.notes)


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
