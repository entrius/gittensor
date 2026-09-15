# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The in-lease watch over the fake box: the heartbeat passes on an honest box; a swapped card, a changed power limit
or NVML lib, a restarted or recreated container, a foreign GPU process, a vanished or exited container each bench the
box, withhold its pay and undeploy its instances; a heartbeat with no answer benches nothing; health failures below
the threshold do nothing and at it replace the replica with a standing event, and the reconciler starts the
replacement; the state store keeps an operator's admit written beside the controller."""

import pytest

from gittensor.controller.checks.runner import regex
from gittensor.controller.checks.state import (
    BENCHED,
    CHECKING,
    DRAINING,
    HEALTH_FAILED,
    HEARTBEAT_FAILED,
    IDLE,
    LEASED,
    STARTING,
    BoxState,
    StateStore,
    transition_card,
)
from gittensor.controller.heartbeat import (
    DEVICE_HOLDERS_COMMAND,
    Watch,
    parse_cgroups,
    parse_compute_apps,
    parse_device_holders,
)
from gittensor.controller.locks import BoxLocks
from gittensor.controller.reconcile import InstanceStore
from gittensor.controller.ssh import SshTransportError
from tests.controller.conftest import NVML_MD5, UUID_5090, UUID_5090_B
from tests.controller.test_placement import Clock, FakeDocker, idle_box, make_world, reconciler, seed


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def leased(world, **docker):
    """One replica LEASED on card UUID_5090 of a two-card box whose last full check recorded its identity."""
    root, registry = world
    box_state = idle_box()
    box_state.identity = {'power_limits': {UUID_5090: 575.0, UUID_5090_B: 575.0}, 'nvml_md5': NVML_MD5}
    seed(root, box_state, replicas=1)
    box = FakeDocker(**docker)
    clock = Clock()
    rec = reconciler(root, registry, {'hk1': box}, clock=clock)
    assert rec.run_pass().ok
    watch = Watch(
        rec.boxes, rec.instances, registry, make_runner=lambda b: box.runner, clock=clock, wall=clock, lock=rec._lock
    )
    (record,) = rec.instances.instances.values()
    return rec, watch, box, clock, record


def test_the_start_records_what_the_heartbeat_holds_the_container_to(world):
    rec, watch, box, clock, record = leased(world)
    container = box.containers[record.container_id]
    assert (record.docker_started_at, record.image_id) == (container['started_at'], container['image_id'])
    assert record.health_ok and record.last_health_at == record.leased_at


def test_the_heartbeat_passes_on_an_honest_box_and_records_the_pay_conditions(world):
    rec, watch, box, clock, record = leased(world)
    report = watch.run_pass()
    assert report.ok and [a.kind for a in report.actions] == ['heartbeat']  # health is not due for 60 s
    current = rec.instances.instances[record.id]
    assert current.heartbeat_ok is True and current.last_heartbeat_at == clock.t
    beat = current.heartbeat
    assert beat['same_card']['ok'] and beat['our_container']['ok'] and beat['card_ours_alone']['ok']
    assert beat['card_ours_alone']['pids'] == [4001]  # the workload's process, mapped to our container by its cgroup
    assert beat['pay'] == {'we_started': True, 'blessed_digest': True, 'healthy': True, 'heartbeat': True}
    assert InstanceStore(rec.instances.path).instances[record.id].heartbeat_ok is True  # in instances.json

    assert watch.run_pass().visited == []  # nothing due inside the interval
    clock.t += 60
    assert watch.run_pass().visited == ['hk1']


def _swap_card(box, record):
    box.gpus = ['GPU-00000000-0000-4000-8000-000000000000', UUID_5090_B]


def _power_cap(box, record):
    box.power_w = 450.0


def _nvml_lib(box, record):
    box.nvml_md5 = 'f' * 32


def _restart(box, record):
    box.restart(record.container_id)


def _recreate(box, record):
    box.recreate(record.container_id)


def _foreign_container(box, record):
    box.gpu_process(record.uuid, 'c' * 64)


def _host_process(box, record):
    box.gpu_process(record.uuid, None)


def _hidden_process(box, record):
    box.hidden.add(box.gpu_process(record.uuid, record.container_id))


def _vanish(box, record):
    box.containers.pop(record.container_id)


def _exit(box, record):
    box.containers[record.container_id]['state'] = 'exited'


@pytest.mark.parametrize(
    'tamper, failed, why',
    [
        (_swap_card, 'same_card', 'pinned card(s) missing'),
        (_power_cap, 'same_card', 'power limit 450.0 W, was 575.0 W'),
        (_nvml_lib, 'same_card', 'NVML lib md5'),
        (_restart, 'our_container', 'restarted: StartedAt'),
        (_recreate, 'our_container+card_ours_alone', 'vanished'),  # its process now sits in a container not ours
        (_foreign_container, 'card_ours_alone', 'pid 4002 in cccccccccccc'),
        (_host_process, 'card_ours_alone', 'pid 4002 in no container'),
        (_hidden_process, 'card_ours_alone', 'pid 4002 not visible on the host'),
        (_vanish, 'our_container', 'vanished'),
        (_exit, 'our_container', 'container exited'),
    ],
)
def test_a_heartbeat_failure_benches_the_box_withholds_pay_and_undeploys_its_instances(world, tamper, failed, why):
    rec, watch, box, clock, record = leased(world)
    tamper(box, record)
    clock.t += 1
    report = watch.run_pass()
    beat = next(a for a in report.actions if a.kind == 'heartbeat')
    assert not beat.ok and why in beat.detail, beat.detail
    bench = next(a for a in report.actions if a.kind == 'bench')
    assert bench.states == [BENCHED] and record.id in bench.detail
    after = StateStore(rec.boxes.path).get('hk1')
    names = failed.split('+')
    assert after.status == BENCHED and after.last_failed == [f'heartbeat:{n}' for n in names] and after.bench_count == 1
    assert after.withheld_from == clock.t and after.cards == {}
    event = after.standing_events[-1]
    assert event['kind'] == HEARTBEAT_FAILED and event['failed'] == names and event['at'] == clock.t
    assert InstanceStore(rec.instances.path).instances == {}
    assert box.containers == {} and not box.commands('docker stop')  # undeployed with a kill, no graceful drain


def test_a_heartbeat_with_no_answer_counts_a_miss_and_benches_nothing(world):
    rec, watch, box, clock, record = leased(world)
    box.runner.on(regex(r'^nvidia-smi --query-gpu'), SshTransportError('10.0.0.1:2200: reset'))
    report = watch.run_pass()
    assert 'reset' in report.unreachable['hk1'] and [a.kind for a in report.actions] == ['miss']
    current = rec.instances.instances[record.id]
    assert current.heartbeat_misses == 1 and current.heartbeat_ok is None  # no heartbeat: no pay, no bench
    assert rec.boxes.boxes['hk1'].status == IDLE and box.containers


def test_three_missed_heartbeats_in_a_row_bench_the_box_for_12_h_off_the_ladder(world):
    rec, watch, box, clock, record = leased(world)
    box.runner.on(regex(r'^nvidia-smi --query-gpu'), SshTransportError('10.0.0.1:2200: timed out'))
    for n in (1, 2):
        report = watch.run_pass()
        assert [a.kind for a in report.actions] == ['miss'] and f'({n}/3 in a row)' in report.actions[0].detail
        asked = len(box.commands('nvidia-smi --query-gpu'))
        clock.t += 5
        watch.run_pass()  # a miss waits out the interval like an answer: no heartbeat retried every tick
        assert len(box.commands('nvidia-smi --query-gpu')) == asked and rec.boxes.boxes['hk1'].unreachable_count == n
        clock.t += 55
    two = rec.boxes.boxes['hk1']
    assert two.status == IDLE and two.unreachable_count == 2 and box.containers  # two misses do nothing

    report = watch.run_pass()
    assert [a.kind for a in report.actions] == ['miss', 'bench']
    assert 'BENCHED for 12 h' in report.actions[1].detail and record.id in report.actions[1].detail
    after = StateStore(rec.boxes.path).get('hk1')
    assert after.status == BENCHED and after.last_failed == ['ssh_unreachable'] and after.bench_count == 0
    assert after.bench_until - after.benched_at == 12 * 3600 and after.withheld_from is None and after.cards == {}
    assert InstanceStore(rec.instances.path).instances == {} and box.containers == {}  # undeployed with a kill
    assert not box.commands('docker stop')


def test_an_answered_heartbeat_resets_the_miss_count(world):
    rec, watch, box, clock, record = leased(world)
    box.runner.on(regex(r'^nvidia-smi --query-gpu'), SshTransportError('10.0.0.1:2200: timed out'))
    for _ in (1, 2):
        watch.run_pass()
        clock.t += 60
    assert rec.boxes.boxes['hk1'].unreachable_count == 2
    box.runner.on(regex(r'^nvidia-smi --query-gpu'), box.respond)
    assert watch.run_pass().ok and rec.boxes.boxes['hk1'].unreachable_count == 0
    box.runner.on(regex(r'^nvidia-smi --query-gpu'), SshTransportError('10.0.0.1:2200: timed out'))
    clock.t += 60
    assert '(1/3 in a row)' in watch.run_pass().actions[0].detail and rec.boxes.boxes['hk1'].status == IDLE


def test_a_record_from_before_ws_d_is_recorded_at_its_first_heartbeat(world):
    rec, watch, box, clock, record = leased(world)
    record.docker_started_at = record.image_id = ''
    rec.instances.put(record)
    assert watch.run_pass().ok
    current = rec.instances.instances[record.id]
    assert current.docker_started_at == box.containers[record.container_id]['started_at']
    assert current.heartbeat['our_container']['recorded_now']
    box.restart(record.container_id)
    clock.t += 60
    assert not watch.run_pass().ok and rec.boxes.boxes['hk1'].status == BENCHED


def test_health_failures_below_the_threshold_do_nothing_and_at_it_replace_the_replica(world):
    rec, watch, box, clock, record = leased(world)
    box.pause(record.container_id)  # the heartbeat still passes: our container, on our card, just not answering
    for n in (1, 2):
        clock.t += 60
        report = watch.run_pass()
        assert [(a.kind, a.ok) for a in report.actions] == [('heartbeat', True), ('health', False)]
        assert f'({n}/3)' in report.actions[1].detail
        current = rec.instances.instances[record.id]
        assert current.health_failures == n and not current.healthy and not current.draining
        box_now = rec.boxes.boxes['hk1']
        assert box_now.cards[record.uuid].state == LEASED and box_now.standing_events == []

    clock.t += 60
    report = watch.run_pass()
    replace = next(a for a in report.actions if a.kind == 'replace')
    assert replace.states == [LEASED, DRAINING, CHECKING] and '3 health failures in a row' in replace.detail
    after = rec.boxes.boxes['hk1']
    assert after.status == IDLE and after.cards[record.uuid].state == CHECKING and after.withheld_from is None
    (event,) = after.standing_events
    assert event['kind'] == HEALTH_FAILED and event['instance'] == record.id and event['failures'] == 3
    assert record.id not in rec.instances.instances and box.containers == {}
    assert box.commands('docker stop --time 5')  # the manifest's drain, not a kill

    box.healthy = True
    report = rec.run_pass()
    (start,) = [a for a in report.actions if a.kind == 'start']
    assert start.ok and start.uuid == UUID_5090_B  # the replacement, elsewhere


def test_a_sleeping_foreign_container_holding_the_device_nodes_benches_the_box(world):
    rec, watch, box, clock, record = leased(world)
    box.hold_devices('c' * 64, comm='sleep')  # `docker run --gpus all ... sleep infinity`: no CUDA context, no NVML pid
    clock.t += 1
    report = watch.run_pass()
    beat = next(a for a in report.actions if a.kind == 'heartbeat')
    assert not beat.ok and 'foreign device holder' in beat.detail and '(sleep) in cccccccccccc' in beat.detail
    assert 'pid 4001' not in beat.detail  # our workload's own handles are not the complaint
    after = StateStore(rec.boxes.path).get('hk1')
    assert after.status == BENCHED and after.last_failed == ['heartbeat:card_ours_alone'] and after.withheld_from
    assert box.containers == {} and 'c' * 64 not in box.runner.calls[-1]  # benched and undeployed; the holder untouched
    assert len(box.commands(DEVICE_HOLDERS_COMMAND)) == 1  # one command in the heartbeat's visit


def test_our_own_device_handles_and_the_hosts_persistence_daemon_pass(world):
    rec, watch, box, clock, record = leased(world)
    box.hold_devices(record.container_id, comm='sleep')  # a second process of our own container
    box.hold_devices(None, comm='nvidia-persiste')  # nvidia-persistenced on the host
    report = watch.run_pass()
    assert report.ok, report.actions
    handles = rec.instances.instances[record.id].heartbeat['card_ours_alone']['device_handles']
    assert handles['ok'] and handles['holders'] == [4001, 4002, 4003]

    box.hold_devices('d' * 64, comm='nvidia-persiste')  # the name alone does not pass inside a container
    clock.t += 60
    assert not watch.run_pass().ok and rec.boxes.boxes['hk1'].status == BENCHED


def test_the_device_scan_waits_while_a_start_drain_or_proof_holds_the_box(world):
    rec, watch, box, clock, record = leased(world)
    watch.box_locks = BoxLocks()
    box.hold_devices('e' * 64, comm='gt_proof')  # a proof container on the other card, mid-round: not recorded
    watch.box_locks.acquire('hk1')
    report = watch.run_pass()
    assert report.ok and not box.commands(DEVICE_HOLDERS_COMMAND)
    beat = rec.instances.instances[record.id].heartbeat
    assert beat['card_ours_alone']['device_handles']['ok'] is None and beat['pay']['heartbeat'] is True
    watch.box_locks.release('hk1')
    clock.t += 60
    assert not watch.run_pass().ok  # still there once the box is free: foreign


def test_device_holder_parsing():
    ours = 'a' * 64
    holders = parse_device_holders(
        '/proc/1/root/proc/10/fd /dev/nvidia0\n/proc/1/root/proc/10/fd /dev/nvidiactl\n'
        '/proc/1/root/proc/10/fd /dev/nvidiactl\n/proc/1/root/proc/11/fd /dev/nvidia-modeset\n'
        '/proc/1/root/proc/12/fd /dev/nvidia-uvm\n/proc/1/root/proc/13/fd /dev/nvidia1\n'
        f'== 10 python3\n0::/system.slice/docker-{ours}.scope\n== 12 sleep\nMISSING\n'
    )
    assert sorted(holders) == [10, 12, 13]  # nvidia-modeset is not a node a GPU job holds
    assert holders[10].devices == ['/dev/nvidia0', '/dev/nvidiactl'] and holders[10].containers == {ours}
    assert holders[10].comm == 'python3' and holders[12].read and holders[12].containers is None
    assert not holders[13].read  # no block came back for it: fails closed in the judge


def test_the_watch_leaves_cards_that_are_not_leased_alone(world):
    root, registry = world
    seed(root, transition_card(idle_box(), UUID_5090, STARTING, 1.0, 'i-000000000001'), replicas=0)
    box = FakeDocker()
    watch = Watch(
        StateStore(root / 'boxes.json'), InstanceStore(root / 'instances.json'), registry, lambda b: box.runner
    )
    assert watch.run_pass().visited == [] and box.runner.calls == []


def test_compute_apps_and_cgroup_parsing():
    apps, odd = parse_compute_apps('123, GPU-4f2a-1\n[N/A], GPU-4f2a-2\n\nNo running processes found\n')
    assert apps == [(123, 'GPU-4f2a-1')] and odd == ['[N/A], GPU-4f2a-2']
    ours, pod = 'a' * 64, 'b' * 64
    parsed = parse_cgroups(
        f'== 123\n0::/system.slice/docker-{ours}.scope\n== 124\nMISSING\n== 125\n0::/user.slice\n'
        f'== 126\n0::/docker/{pod}/docker/{ours}\n'  # docker in docker: the pod's ID and ours
    )
    assert parsed == {123: {ours}, 124: None, 125: set(), 126: {pod, ours}}


def test_the_state_store_keeps_an_operators_admit_written_beside_it(tmp_path):
    path = tmp_path / 'boxes.json'
    ours = StateStore(path)
    ours.put(idle_box())
    theirs = StateStore(path)  # `gitt controller admit` beside a running controller
    moved = theirs.get('hk1')
    moved.host = '10.9.9.9'
    theirs.put(moved)
    theirs.put(BoxState('hk2', host='10.0.0.2', port=2200))
    ours.put(transition_card(ours.boxes['hk1'], UUID_5090, STARTING, 1.0, 'i-000000000001'))
    final = StateStore(path)
    assert set(final.boxes) == {'hk1', 'hk2'} and final.get('hk1').host == '10.9.9.9'
    assert final.get('hk1').cards[UUID_5090].state == STARTING  # card state stays the controller's
