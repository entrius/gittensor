# The MIT License (MIT)
# Copyright © 2025 Entrius

"""gitt controller over fakes: admit pins a host key and refuses a changed one; check prints ADMIT for the passing box
and BENCH with reasons for a failing one, exiting 0 / 1 / 2; the proof is loaded by import path; round stages every
box before firing any and benches a UUID two boxes claim; the state file round-trips."""

import base64
import importlib
import json
import shutil
import subprocess
import textwrap
import threading
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gittensor.cli.main import cli
from gittensor.controller import cli as ctl
from gittensor.controller.checks import checks as ck
from gittensor.controller.checks.runner import FakeRunner, regex
from gittensor.controller.checks.state import ADMIT, BENCHED, IDLE, BoxState, StateStore, release_from_bench
from gittensor.controller.proof.slot import ProbeResult, UnconfiguredProof
from gittensor.controller.ssh import SshTransportError
from tests.controller.conftest import (
    AGENT_DIGEST,
    AGENT_IMAGE_ID,
    DRIVER,
    FIXTURES,
    NETWORK_TARGETS,
    NVML_MD5,
    UUID_5090,
    UUID_5090_B,
    failing,
    fixture,
    passing_runner,
)

HK_A, HK_B = '5HotkeyA', '5HotkeyB'
KEY_1 = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOne'
KEY_2 = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITwo'
FAKE_PROOF = 'tests.controller.conftest:FakeProof'
NET = [arg for url in NETWORK_TARGETS for arg in ('--network-target', url)]


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


def invoke(*args):
    return CliRunner().invoke(cli, ['controller', *map(str, args)])


def admit(state, hotkey=HK_A, host='10.0.0.1', port=2200, key=KEY_1, extra=()):
    with patch.object(ctl, '_scan_host_key', return_value=key):
        return invoke('admit', hotkey, '--host', host, '--port', port, '--state-dir', state, *extra)


def runners(by_box):
    return patch.object(ctl, '_make_runner', side_effect=lambda st, box, ca, purpose: by_box[box.box_id])


def box_runner(**kw):
    return passing_runner(**kw).on('true', '')


def check(state, *extra, hotkey=HK_A):
    return invoke('check', hotkey, '--state-dir', state, *NET, *extra)


def store(state):
    return StateStore(state / 'boxes.json')


def known_hosts(state):
    return (state / 'known_hosts').read_text()


# ---------------------------------------------------------------- admit ---------------------------------------------


def test_admit_pins_the_host_key_and_refuses_a_changed_one(state):
    result = admit(state)
    assert result.exit_code == 0, result.output
    box = store(state).get(HK_A)
    assert box.status == ADMIT and (box.host, box.port, box.host_key) == ('10.0.0.1', 2200, KEY_1)
    assert known_hosts(state) == f'[10.0.0.1]:2200 {KEY_1}\n'
    assert admit(state).exit_code == 0 and known_hosts(state) == f'[10.0.0.1]:2200 {KEY_1}\n'  # same key: a no-op

    result = admit(state, key=KEY_2)
    assert result.exit_code == 1 and 'changed' in result.output and '--force-rekey' in result.output
    assert store(state).get(HK_A).host_key == KEY_1 and KEY_2 not in known_hosts(state)
    # another hotkey cannot slip a different key onto an address already pinned
    assert admit(state, hotkey=HK_B, key=KEY_2).exit_code == 1 and HK_B not in store(state).boxes

    result = admit(state, key=KEY_2, extra=['--force-rekey', '--json'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['rekeyed'] is True
    assert known_hosts(state) == f'[10.0.0.1]:2200 {KEY_2}\n' and store(state).get(HK_A).host_key == KEY_2


def test_admit_keeps_state_and_moves_the_known_hosts_entry(state):
    admit(state)
    s = store(state)
    s.put(BoxState.from_dict({**s.get(HK_A).as_dict(), 'status': IDLE, 'pinned_uuids': [UUID_5090]}))
    assert admit(state, host='10.0.0.2', port=2222).exit_code == 0
    box = store(state).get(HK_A)
    assert box.status == IDLE and box.pinned_uuids == [UUID_5090] and (box.host, box.port) == ('10.0.0.2', 2222)
    assert known_hosts(state) == f'[10.0.0.2]:2222 {KEY_1}\n'


def test_admit_unreachable_box_exits_2(state):
    with patch.object(ctl, '_scan_host_key', side_effect=SshTransportError('ssh-keyscan 10.0.0.9:2200: no key')):
        result = invoke('admit', HK_A, '--host', '10.0.0.9', '--state-dir', state)
    assert result.exit_code == 2 and 'no key' in result.output and not (state / 'boxes.json').exists()


# ---------------------------------------------------------------- check ---------------------------------------------


def test_check_admits_the_passing_box_and_pins_it(state):
    admit(state)
    with runners({HK_A: box_runner()}):
        result = check(state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST)
    assert result.exit_code == 0, result.output
    out = result.output
    assert 'ADMIT' in out and 'ADMIT → IDLE' in out and 'fleet_uuid_unique' in out and 'fake-1' in out
    box = store(state).get(HK_A)
    assert box.status == IDLE and box.pinned_uuids == [UUID_5090] and box.last_check_at is not None

    with runners({HK_A: box_runner()}):
        result = check(state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST, '--json')
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload['verdict'] == 'ADMIT' and payload['status'] == {'before': IDLE, 'after': IDLE}
    assert [c['name'] for c in payload['checks']] == [
        ck.GPU_SPEC,
        ck.GPU_UUID_PIN,
        ck.FLEET_UUID_UNIQUE,
        ck.NVML_DIGEST,
        ck.POWER_LIMIT,
        ck.AGENT_IMAGE,
        ck.DISK_FREE,
        ck.CARD_FREE,
        ck.NETWORK,
        ck.GPU_PROOF,
    ]
    assert {'connect', 'scrape', 'stage', 'fire', 'cleanup', 'total'} <= set(payload['timings_ms'])
    assert ck.GPU_PROOF in payload['check_ms'] and payload['provider'] == 'fake-1'


def test_check_benches_a_failing_box_with_reasons(state):
    admit(state)
    with runners({HK_A: box_runner(agent_image='')}):  # a local build: no repo digest, and no ID pinned
        result = check(state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST)
    assert result.exit_code == 1, result.output
    assert 'BENCH' in result.output and 'no repo digest' in result.output
    box = store(state).get(HK_A)
    assert box.status == BENCHED and box.last_failed == [ck.AGENT_IMAGE] and box.bench_count == 1
    # a benched box is not checked again until its bench expires
    with runners({HK_A: box_runner()}):
        result = check(state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST, '--json')
    assert result.exit_code == 1 and 'BENCHED until' in result.stdout


def test_dev_image_id_satisfies_the_agent_check_and_nothing_pinned_does_not(state):
    admit(state)
    with runners({HK_A: box_runner(agent_image='')}):
        result = check(state, '--proof', FAKE_PROOF, '--agent-image-id', AGENT_IMAGE_ID.split(':')[1], '--json')
    assert result.exit_code == 0, result.output
    agent = next(c for c in json.loads(result.stdout)['checks'] if c['name'] == ck.AGENT_IMAGE)
    assert agent['pass'] and agent['evidence']['matched'] == 'image_id (dev)'

    assert not ck.check_agent_image([], [], '', AGENT_IMAGE_ID, []).passed  # nothing pinned: fail closed
    other = ck.check_agent_image([], [], '', AGENT_IMAGE_ID, ['sha256:' + 'c' * 64])
    assert not other.passed and 'neither' in other.evidence['reason']
    assert ck.check_agent_image(['sha256:' + 'a' * 64], ['sha256:' + 'a' * 64]).passed  # prod path unchanged


def test_no_provider_admits_nobody_and_is_a_strike_with_the_reason_named(state):
    admit(state)
    with runners({HK_A: box_runner()}):
        result = check(state, '--agent-image-digest', AGENT_DIGEST, '--json')
    assert result.exit_code == 2  # no answer was judged: not a bench
    out = json.loads(result.stdout)
    proof = next(c for c in out['checks'] if c['name'] == ck.GPU_PROOF)
    assert not proof['pass'] and proof['not_run'] and 'no GPU proof provider configured' in proof['evidence']['reason']
    assert out['verdict'] == 'NOT_RUN' and out['failed'] == [] and out['not_run'] == [ck.GPU_PROOF]
    box = store(state).get(HK_A)
    assert box.status == ADMIT and box.not_run_count == 1 and box.bench_count == 0


def test_transport_failure_exits_2_counts_and_benches_after_three(state):
    admit(state)
    dead = FakeRunner().on('true', SshTransportError('10.0.0.1:2200: Connection refused'))
    with runners({HK_A: dead}):
        result = check(state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST)
    assert result.exit_code == 2 and 'Connection refused' in result.output and '1 round(s)' in result.output
    box = store(state).get(HK_A)
    assert box.status == ADMIT and box.last_check_at is None and box.unreachable_count == 1
    # lost mid-scrape is the same: no verdict, the count goes up
    flaky = box_runner().on(
        r'nvidia-smi --query-gpu=uuid,name,driver_version,memory.total,power.limit,power.default_limit,power.max_limit,pci.bus_id,compute_cap --format=csv,noheader,nounits',
        SshTransportError('reset'),
    )
    with runners({HK_A: flaky}):
        result = check(state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST, '--json')
    assert result.exit_code == 2 and 'reset' in result.stdout and store(state).get(HK_A).unreachable_count == 2
    # third in a row: BENCHED for a flat 12 h, no fraud-ladder rung consumed
    with runners({HK_A: dead}):
        result = check(state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST)
    assert result.exit_code == 2 and 'BENCHED for 12 h' in result.output
    box = store(state).get(HK_A)
    assert box.status == BENCHED and box.last_failed == ['ssh_unreachable'] and box.bench_count == 0
    assert box.bench_until is not None and box.benched_at is not None
    assert box.bench_until - box.benched_at == 12 * 3600
    # a verdict resets the count
    s = store(state)
    s.put(BoxState.from_dict({**box.as_dict(), 'status': ADMIT, 'bench_until': None}))
    with runners({HK_A: box_runner()}):
        assert check(state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST).exit_code == 0
    assert store(state).get(HK_A).unreachable_count == 0


def test_force_checks_a_benched_box_without_touching_the_bench(state):
    admit(state)
    s = store(state)
    benched = BoxState.from_dict(
        {
            **s.get(HK_A).as_dict(),
            'status': BENCHED,
            'bench_until': 9e12,
            'bench_count': 1,
            'last_failed': ['gpu_proof'],
        }
    )
    s.put(benched)
    with runners({HK_A: box_runner()}):
        refused = check(state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST)
        forced = check(state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST, '--force')
    assert refused.exit_code == 1 and 'not checked' in refused.output
    assert forced.exit_code == 0 and '--force' in forced.output and 'ADMIT' in forced.output
    after = store(state).get(HK_A)
    assert after.status == BENCHED and after.bench_until == 9e12 and after.last_failed == ['gpu_proof']


def test_remove_forgets_a_box_and_its_host_key_but_not_one_that_carries_an_instance(state):
    from gittensor.controller.reconcile import InstanceRecord, InstanceStore
    from gittensor.controller.ssh import pinned_host_key

    assert invoke('remove', HK_A, '--state-dir', state).exit_code != 0  # not admitted
    admit(state)
    assert pinned_host_key(state / 'known_hosts', '10.0.0.1', 2200) == KEY_1
    InstanceStore(state / 'instances.json').put(InstanceRecord('i-1', 'e@1', HK_A, UUID_5090))
    refused = invoke('remove', HK_A, '--state-dir', state)
    assert refused.exit_code == 1 and 'drain it first' in refused.output
    assert HK_A in store(state).boxes
    InstanceStore(state / 'instances.json').remove('i-1')
    result = invoke('remove', HK_A, '--reason', 'pod returned', '--state-dir', state, '--json')
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {'success': True, 'hotkey': HK_A, 'reason': 'pod returned', 'removed': True, 'pending': False}  # fmt: skip
    assert HK_A not in store(state).boxes
    assert not pinned_host_key(state / 'known_hosts', '10.0.0.1', 2200)
    assert invoke('remove', HK_A, '--state-dir', state).exit_code != 0  # gone: not admitted any more


def test_release_ends_a_bench_early_and_refuses_a_box_that_is_not_benched(state):
    admit(state)
    refused = invoke('release', HK_A, '--state-dir', state)
    assert refused.exit_code == 1 and 'not benched' in refused.output
    s = store(state)
    s.put(
        BoxState.from_dict(
            {**s.get(HK_A).as_dict(), 'status': BENCHED, 'benched_at': 5.0, 'bench_until': 9e12, 'bench_count': 2}
        )
    )
    result = invoke('release', HK_A, '--reason', 'driver reinstalled, checked by hand', '--state-dir', state, '--json')
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload['released'] is True and payload['pending'] is False and payload['status'] == ADMIT
    box = store(state).get(HK_A)
    assert box.status == ADMIT and box.bench_until is None and box.bench_count == 2  # the ladder rung stays
    (event,) = box.standing_events
    assert event['kind'] == 'released' and event['reason'] == 'driver reinstalled, checked by hand'
    assert event['bench_until'] == 9e12 and event['requested_at'] == box.release_request['at']

    with runners({HK_A: box_runner()}):  # re-pinned by the next check, like an expired bench
        assert check(state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST).exit_code == 0
    box = store(state).get(HK_A)
    assert box.status == IDLE and box.pinned_uuids == [UUID_5090] and len(box.standing_events) == 1


def test_release_forgive_gives_the_rung_back(state):
    admit(state)
    s = store(state)
    benched = {
        'status': BENCHED,
        'benched_at': 5.0,
        'bench_until': 9e12,
        'bench_count': 2,
        'last_failed': ['nvml_digest'],
    }
    events = [{'at': 5.0, 'kind': 'check_failed', 'failed': ['nvml_digest']}]
    s.put(BoxState.from_dict({**s.get(HK_A).as_dict(), **benched, 'standing_events': events}))
    result = invoke('release', HK_A, '--forgive', '--reason', 'our allowlist', '--state-dir', state, '--json')
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['forgiven'] is True
    box = store(state).get(HK_A)
    assert box.status == ADMIT and box.bench_count == 1
    (event,) = box.standing_events  # the bench's own event is gone
    assert event['kind'] == 'released' and event['forgiven'] is True
    assert invoke('release', HK_A, '--state-dir', state).exit_code == 1  # IDLE: nothing to release

    # the request is for that bench only: a later bench is not lifted by it
    later = BoxState.from_dict({**box.as_dict(), 'status': BENCHED, 'benched_at': 9e11, 'bench_until': 9e12})
    assert release_from_bench(later, 9e11 + 1).status == BENCHED


def test_unadmitted_box_and_missing_ca_key_exit_2(state):
    assert check(state).exit_code == 2
    admit(state)
    (state / 'gt_ca').unlink()
    result = check(state, '--json')
    assert result.exit_code == 2 and 'CA private key not found' in result.stdout


def test_proof_is_loaded_by_import_path(state, tmp_path, monkeypatch):
    (tmp_path / 'fake_provider_mod.py').write_text(
        textwrap.dedent(
            """
            from tests.controller.conftest import FakeProof

            made = []

            class Recording(FakeProof):
                def __init__(self, secret_store, version, binary_path):
                    super().__init__(version=version)
                    made.append({'secret_store': secret_store, 'version': version, 'binary_path': binary_path})
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    secrets = tmp_path / 'secret_store.json'
    secrets.write_text(json.dumps({'v-1': base64.b64encode(b'\x01' * 32).decode()}))
    (tmp_path / 'version').write_text('v-1\n')
    args = [f'secret_store={secrets}', f'version=@{tmp_path / "version"}', 'binary_path=/x/gt_proof']

    proof = ctl.load_proof('fake_provider_mod:Recording', args)
    fake_provider_mod = importlib.import_module('fake_provider_mod')

    assert proof.version == 'v-1'
    assert fake_provider_mod.made == [
        {'secret_store': {'v-1': b'\x01' * 32}, 'version': 'v-1', 'binary_path': '/x/gt_proof'}
    ]
    assert isinstance(ctl.load_proof(None), UnconfiguredProof)
    for spec, pairs in (
        ('no-colon', []),
        ('no_such_module_xyz:X', []),
        ('fake_provider_mod:Missing', []),
        ('fake_provider_mod:Recording', ['no-equals']),
        ('fake_provider_mod:Recording', []),  # the constructor refuses: named, not raised raw
        (None, ['version=1']),
    ):
        with pytest.raises(ctl.ProofLoadError):
            ctl.load_proof(spec, pairs)

    admit(state)
    proof_args = [a for pair in args for a in ('--proof-args', pair)]
    with runners({HK_A: box_runner()}):
        result = check(
            state, '--proof', 'fake_provider_mod:Recording', *proof_args, '--agent-image-digest', AGENT_DIGEST, '--json'
        )
    assert result.exit_code == 0, result.output  # built from the loaded kwargs, it seals and judges as v-1
    assert json.loads(result.stdout)['provider'] == 'v-1'
    with runners({HK_A: box_runner()}):
        result = check(state, '--proof', 'fake_provider_mod:Nope', '--json')
    assert result.exit_code == 2 and 'Nope' in result.stdout


# ---------------------------------------------------------------- round ---------------------------------------------


class Logged:
    """Records every command of every box, in order, into one shared list."""

    def __init__(self, name, inner, events, lock):
        self.name, self.inner, self.events, self.lock = name, inner, events, lock

    def run(self, command, timeout=None, stdin=None):
        with self.lock:
            self.events.append((self.name, command))
        return self.inner.run(command, timeout=timeout, stdin=stdin)


def two_boxes(state, smi_b):
    events, lock = [], threading.Lock()
    admit(state, HK_A, '10.0.0.1')
    admit(state, HK_B, '10.0.0.2', key=KEY_2)
    by_box = {
        HK_A: Logged(HK_A, box_runner(), events, lock),
        HK_B: Logged(HK_B, box_runner(nvidia_smi=smi_b), events, lock),
    }
    return events, by_box


def round_args(state, *extra):
    return ['round', '--state-dir', state, '--proof', FAKE_PROOF, '--agent-image-digest', AGENT_DIGEST, *NET, *extra]


def test_round_stages_every_box_before_firing_any(state):
    events, by_box = two_boxes(state, fixture('nvidia_smi_5090.csv').replace(UUID_5090, UUID_5090_B))
    with runners(by_box):
        result = invoke(*round_args(state, '--json'))
    assert result.exit_code == 0, result.output
    stage = [i for i, (_, c) in enumerate(events) if c.startswith(('docker create', 'docker cp'))]
    fire = [i for i, (_, c) in enumerate(events) if c.startswith('docker start')]
    assert {events[i][0] for i in stage} == {HK_A, HK_B} == {events[i][0] for i in fire}
    assert max(stage) < min(fire)  # both boxes fully staged before either is fired
    payload = json.loads(result.stdout)
    assert [b['verdict'] for b in payload['boxes']] == ['ADMIT', 'ADMIT'] and payload['provider'] == 'fake-1'
    assert {'connect_scrape', 'stage', 'fire', 'cleanup', 'total', 'fire_spread'} <= set(payload['timings_ms'])
    assert store(state).get(HK_A).pinned_uuids == [UUID_5090] and store(state).get(HK_B).pinned_uuids == [UUID_5090_B]


def test_a_box_without_the_proof_image_gets_no_verdict_and_its_pull_is_started(state):
    """The proof image is ~2 GB: `docker create` pulled it inside the proof's 60 s timeout and the timeout was judged
    as a failed GPU proof (mainnet 9/18). A missing image is setup: no verdict, no bench, the pull started detached;
    the box beside it is proved as usual, and this one on the first round that finds the image."""
    events, by_box = two_boxes(state, fixture('nvidia_smi_5090.csv').replace(UUID_5090, UUID_5090_B))
    by_box[HK_B].inner.on(regex(r'^if docker image inspect '), 'pulling\n')
    with runners(by_box):
        result = invoke(*round_args(state, '--json'))
    rows = {b['hotkey']: b for b in json.loads(result.stdout)['boxes']}
    assert rows[HK_A]['verdict'] == 'ADMIT' and rows[HK_B]['verdict'] is None
    assert 'proof image not on the box yet' in rows[HK_B]['busy']
    assert store(state).get(HK_B).status == ADMIT and not store(state).get(HK_B).standing_events  # not benched
    on_b = [c for hk, c in events if hk == HK_B]
    assert any('nohup docker pull -q' in c for c in on_b) and not any(c.startswith('docker create') for c in on_b)

    by_box[HK_B].inner.on(regex(r'^if docker image inspect '), 'ready\n')  # the pull finished
    with runners(by_box):
        result = invoke(*round_args(state, '--json'))
    rows = {b['hotkey']: b for b in json.loads(result.stdout)['boxes']}
    assert rows[HK_B]['verdict'] == 'ADMIT' and store(state).get(HK_B).status == IDLE


OCI_ERROR = (
    'Error response from daemon: failed to create task for container: failed to create shim task: OCI runtime create '
    'failed: runc create failed: unable to start container process: error during container init: error running '
    'prestart hook #0: exit status 1, stdout: , stderr: nvidia-container-cli: initialization error: nvml error: '
    'driver/library version mismatch: unknown'
)


def test_a_proof_container_that_will_not_start_is_a_strike_tried_once_a_round(state):
    """Mainnet 9/19: NVIDIA's prestart hook failed on the first outside miner's box; the proof never ran and the box
    was benched 64 h as a failed GPU proof. Now: a strike (no bench, the box beside it proved as usual), the full
    reason in the log, one try a round, and the third strike in a row is a bench on the ladder."""
    events, by_box = two_boxes(state, fixture('nvidia_smi_5090.csv').replace(UUID_5090, UUID_5090_B))
    by_box[HK_B].inner.on(regex(r'^docker start '), failing(OCI_ERROR))
    with runners(by_box):
        result = invoke(*round_args(state, '--json'))
    assert result.exit_code == 2, result.output  # no answer was judged
    rows = {b['hotkey']: b for b in json.loads(result.stdout)['boxes']}
    assert rows[HK_A]['verdict'] == 'ADMIT'
    b = rows[HK_B]
    assert (b['verdict'], b['failed'], b['not_run'], b['strikes']) == ('NOT_RUN', [], [ck.GPU_PROOF], 1)
    reason = next(c for c in b['checks'] if c['name'] == ck.GPU_PROOF)['evidence']['reason']
    assert reason.endswith('driver/library version mismatch: unknown')  # not cut off at 300
    box = store(state).get(HK_B)
    assert box.status == ADMIT and box.bench_count == 0 and box.not_run_count == 1
    assert [e['kind'] for e in box.standing_events] == ['check_not_run']

    # the next round comes before the retry is due: the box is left alone
    events.clear()
    with runners(by_box):
        result = invoke(*round_args(state, '--json'))
    rows = {b['hotkey']: b for b in json.loads(result.stdout)['boxes']}
    assert rows[HK_B]['verdict'] is None and 'strike 1 of 3' in rows[HK_B]['busy']
    assert not any(c.startswith('docker create') for hk, c in events if hk == HK_B)
    assert store(state).get(HK_B).not_run_count == 1

    def make_due():
        s = store(state)
        s.put(BoxState.from_dict({**s.get(HK_B).as_dict(), 'not_run_at': 0.0}))

    for strikes, status in ((2, ADMIT), (0, BENCHED)):
        make_due()
        with runners(by_box):
            invoke(*round_args(state, '--json'))
        box = store(state).get(HK_B)
        assert (box.not_run_count, box.status) == (strikes, status)
    assert box.bench_count == 1 and box.last_failed == [ck.GPU_PROOF]
    assert box.standing_events[-1]['kind'] == 'check_failed'


def test_a_strike_clears_when_the_proof_runs_again(state):
    events, by_box = two_boxes(state, fixture('nvidia_smi_5090.csv').replace(UUID_5090, UUID_5090_B))
    by_box[HK_B].inner.on(regex(r'^docker start '), failing(OCI_ERROR))
    with runners(by_box):
        invoke(*round_args(state, '--json'))
    s = store(state)
    s.put(BoxState.from_dict({**s.get(HK_B).as_dict(), 'not_run_at': 0.0}))
    events2, fixed = two_boxes(state, fixture('nvidia_smi_5090.csv').replace(UUID_5090, UUID_5090_B))
    with runners(fixed):
        invoke(*round_args(state, '--json'))
    box = store(state).get(HK_B)
    assert box.status == IDLE and box.not_run_count == 0 and box.bench_count == 0


def test_round_benches_a_uuid_claimed_by_two_boxes(state):
    events, by_box = two_boxes(state, fixture('nvidia_smi_5090.csv'))  # both report UUID_5090
    with runners(by_box):
        result = invoke(*round_args(state))
    assert result.exit_code == 1, result.output
    assert 'fleet_uuid_unique' in result.output and not any(c.startswith('docker create') for _, c in events)
    assert all(store(state).get(hk).status == BENCHED for hk in (HK_A, HK_B))


def test_round_skips_benched_boxes_and_releases_expired_ones(state):
    admit(state, HK_A, '10.0.0.1')
    admit(state, HK_B, '10.0.0.2', key=KEY_2)
    s = store(state)
    s.put(BoxState.from_dict({**s.get(HK_A).as_dict(), 'status': BENCHED, 'bench_until': 1.0, 'bench_count': 1}))
    s.put(BoxState.from_dict({**s.get(HK_B).as_dict(), 'status': BENCHED, 'bench_until': 9e12, 'bench_count': 1}))
    with runners({HK_A: box_runner()}):
        result = invoke(*round_args(state, '--json'))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [(b['hotkey'], b['status']) for b in payload['boxes']] == [(HK_A, {'before': BENCHED, 'after': IDLE})]
    assert payload['not_probed'][0]['hotkey'] == HK_B and store(state).get(HK_B).status == BENCHED


def test_a_round_no_box_answers_counts_nobody_unreachable_and_one_dead_box_still_counts(state):
    admit(state, HK_A, '10.0.0.1')
    admit(state, HK_B, '10.0.0.2', key=KEY_2)
    dead = FakeRunner().on('true', SshTransportError('Connection refused'))
    with runners({HK_A: dead, HK_B: dead}):  # the controller's own link is down: every dial fails
        result = invoke(*round_args(state, '--json'))
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload['no_box_answered'] is True and all(b['transport_error'] for b in payload['boxes'])
    assert [store(state).get(hk).unreachable_count for hk in (HK_A, HK_B)] == [0, 0]
    with runners({HK_A: dead, HK_B: dead}):
        assert 'no box answered SSH' in invoke(*round_args(state)).output

    with runners({HK_A: dead, HK_B: box_runner()}):  # one box down: that box is counted, as before
        result = invoke(*round_args(state, '--json'))
    assert result.exit_code == 2 and json.loads(result.stdout)['no_box_answered'] is False
    assert [store(state).get(hk).unreachable_count for hk in (HK_A, HK_B)] == [1, 0]
    assert store(state).get(HK_B).status == IDLE


def test_round_loop_runs_the_build_between_rounds_and_reloads_the_proof(state, tmp_path, monkeypatch):
    (tmp_path / 'fake_loop_provider.py').write_text(
        'from tests.controller.conftest import FakeProof\n\nmade = []\n\n\n'
        'class Counting(FakeProof):\n    def __init__(self):\n        super().__init__()\n        made.append(1)\n'
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    admit(state)
    builds, sleeps = [], []
    with (
        runners({HK_A: box_runner()}),
        patch.object(
            ctl,
            '_run_build',
            side_effect=lambda c: builds.append(c) or subprocess.CompletedProcess(c, 0, 'built v2\n', ''),
        ),
        patch.object(ctl, '_sleep', side_effect=sleeps.append),
    ):
        result = invoke(
            'round',
            '--state-dir',
            state,
            '--proof',
            'fake_loop_provider:Counting',
            '--agent-image-digest',
            AGENT_DIGEST,
            *NET,
            '--loop',
            '--max-rounds',
            2,
            '--interval',
            0,
            '--build-cmd',
            'ci/gen_secret.sh && ci/build.sh',
        )
    assert result.exit_code == 0, result.output
    fake_loop_provider = importlib.import_module('fake_loop_provider')

    assert builds == ['ci/gen_secret.sh && ci/build.sh'] and len(sleeps) == 1 and len(fake_loop_provider.made) == 2
    assert result.output.count('gitt controller round') == 2 and 'build exit 0' in result.output


def test_empty_probe_is_never_a_pass():
    assert not ck.proof_result(ProbeResult('p')).passed


# ---------------------------------------------------------------- allowlist + state ---------------------------------


def test_allowlist_add_and_show(state):
    (state / 'nvml_allowlist.json').unlink()
    admit(state)
    with runners({HK_A: box_runner()}):
        result = invoke('allowlist', 'add', HK_A, '--state-dir', state, '--json')
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['added'] is True
    assert json.loads((state / 'nvml_allowlist.json').read_text()) == {DRIVER: [NVML_MD5]}
    with runners({HK_A: box_runner()}):
        again = invoke('allowlist', 'add', HK_A, '--state-dir', state, '--json')
    assert json.loads(again.stdout)['added'] is False
    shown = invoke('allowlist', 'show', '--state-dir', state, '--json')
    assert json.loads(shown.stdout)['drivers'] == {DRIVER: [NVML_MD5]}
    # a box whose nvidia-smi and kernel module disagree adds nothing
    with runners({HK_A: box_runner(kernel_driver='NVRM version: NVIDIA UNIX x86_64 Kernel Module  999.1.1  x\n')}):
        bad = invoke('allowlist', 'add', HK_A, '--state-dir', state)
    assert bad.exit_code == 1 and 'disagrees' in bad.output
    assert json.loads((state / 'nvml_allowlist.json').read_text()) == {DRIVER: [NVML_MD5]}


def test_state_file_round_trips(tmp_path):
    path = tmp_path / 'boxes.json'
    box = BoxState(
        'hk', status=IDLE, pinned_uuids=[UUID_5090], host='1.2.3.4', port=2200, host_key=KEY_1, last_check_at=5.0
    )
    StateStore(path).put(box)
    assert StateStore(path).get('hk') == box
    # a file written before host / port / host_key existed (and with a field from the future) still loads
    old = {k: v for k, v in box.as_dict().items() if k not in ('host', 'port', 'host_key')}
    path.write_text(json.dumps({'hk': {**old, 'someday': 1}}))
    loaded = StateStore(path).get('hk')
    assert loaded.pinned_uuids == [UUID_5090] and (loaded.host, loaded.port, loaded.host_key) == ('', 0, '')


def test_fleet_uuid_unique_judge():
    ok = ck.check_fleet_uuid_unique('a', ['GPU-1'], {'a': {'GPU-1'}, 'b': {'GPU-2'}})
    assert ok.passed and ok.evidence['boxes_compared'] == 1
    dup = ck.check_fleet_uuid_unique('a', ['GPU-1', 'GPU-3'], {'b': ['GPU-3'], 'c': []})
    assert not dup.passed and dup.evidence['clashes'] == {'b': ['GPU-3']}
