# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Rentals (vault 29): the pod's docker lines and firewall, port assignment, and the reconciler's pass over a fake box:
place -> start -> active, the end at ends_at and on order, a pod gone under us, an order nothing fits, the ledger paying
a rental like an instance, the placement reconciler leaving a rental's cards alone, and the public document."""

import json
import re

import pytest

from gittensor.controller import rentals as rt
from gittensor.controller.checks.runner import CommandResult, FakeRunner, regex
from gittensor.controller.checks.state import (
    BENCHED,
    CHECKING,
    IDLE,
    LEASED,
    STARTING,
    BoxState,
    CardState,
    StateStore,
)
from gittensor.controller.pay.ledger import Cursors, accrue
from gittensor.controller.publish import build_fleet
from gittensor.controller.standing import CLEAN_LEASE

NOW = 1_760_000_000.0
HK = '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY'  # a well-formed ss58: publish drops any other id
U1 = 'GPU-11111111-2222-4333-8444-555555555555'
U2 = 'GPU-22222222-2222-4333-8444-555555555555'
CID = 'c' * 64
KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests0000000000000000000000000 alex'


def rentable_box(hk: str = HK, cards=(U1, U2), standing_s: float = 8 * 3600, **kw) -> BoxState:
    kw.setdefault('rent_ports', [31000, 31099])
    kw.setdefault('uid', 45)
    return BoxState(
        hk,
        status=IDLE,
        pinned_uuids=list(cards),
        card_name='NVIDIA GeForce RTX 5090',
        host='203.0.113.7',
        port=2200,
        last_check_at=NOW - 300,
        cards={u: CardState(IDLE, '', NOW - 600) for u in cards},
        standing_events=[{'at': NOW - 7_000, 'kind': CLEAN_LEASE, 'leased_s': standing_s}],
        **kw,
    )


def pod_runner(running: str = 'true') -> FakeRunner:
    """A box where every pod step succeeds."""
    runner = FakeRunner()
    runner.on(regex(r'^docker network inspect'), '')
    runner.on(regex(r'^nsenter -t 1 -m -n -- iptables'), '')
    runner.on(regex(r'^docker image inspect'), CommandResult(1, '', 'No such image'))
    runner.on(regex(r'^docker pull'), 'sha256:abc\n')
    runner.on(regex(r'^docker run -d'), CID + '\n')
    runner.on(regex(r'^docker exec -i'), '')
    runner.on(regex(r"^docker inspect --format '\{\{\.State\.Running\}\}'"), running + '\n')
    runner.on(regex(r'^docker stop'), '')
    return runner


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def reconciler(tmp_path, boxes: StateStore, runner: FakeRunner, clock: Clock, probe=lambda h, p: True, **kw):
    store = rt.RentalStore(tmp_path / 'rentals.json')
    rec = rt.RentalReconciler(
        boxes, store, lambda box: runner, wall=clock, sleep=clock.sleep, probe=probe, background=False, prepull=(), **kw
    )
    return store, rec


@pytest.fixture
def boxes(tmp_path) -> StateStore:
    store = StateStore(tmp_path / 'boxes.json')
    store.put(rentable_box())
    return store


# -- the pod's lines -----------------------------------------------------------------------------------------------------


def test_the_pod_runs_under_sysbox_with_every_card_and_its_ports_on_the_box():
    r = rt.RentalRecord(
        'rnt_1', uuids=[U1, U2], image='ubuntu:24.04', port_map={'22': 31000, '8888': 31001}, env={'A': '1'}
    )
    line = rt.pod_run_command(r)
    assert line.startswith('docker run -d --name gt-rnt_1 --runtime=sysbox-runc')
    assert '--privileged' not in line and '-v ' not in line and '--cap-add' not in line and '--net host' not in line
    assert f'--gpus \'"device={U1},{U2}"\'' in line and f'--label io.gittensor.uuid={U1},{U2}' in line
    assert '--label io.gittensor.rental=rnt_1' in line and '--restart no' in line and '--network gt-rental' in line
    assert '-p 31000:22 -p 31001:8888' in line and '-e A=1' in line and line.endswith(' ubuntu:24.04')
    assert '--shm-size 8g' in line and '--pids-limit 8192' in line


def test_the_pods_bridge_has_icc_off_and_the_firewall_keeps_the_miners_lan_out():
    net = rt.ensure_rental_network_command()
    assert 'enable_icc=false' in net and 'bridge.name=gt-rental' in net and 'docker network create' in net
    rules = rt.firewall_rules()
    for private in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '100.64.0.0/10'):
        assert f'DOCKER-USER -i gt-rental -d {private} -j DROP' in rules
    assert any('--dport 25 -j DROP' in r for r in rules) and any('hashlimit-above 200/sec' in r for r in rules)
    line = rt.firewall_commands()
    # every rule is asked for before it is added, on the host's namespaces, so a re-run adds nothing
    assert line.count('iptables -w -C') == len(rules) == line.count('iptables -w -I')
    assert line.count('nsenter -t 1 -m -n --') == 2 * len(rules)


def test_keys_go_in_over_stdin_and_never_on_a_command_line():
    cmd = rt.authorized_keys_command(CID, [KEY])
    assert KEY not in cmd and 'docker exec -i' in cmd and 'authorized_keys' in cmd
    assert rt.keys_stdin([KEY, '', ' ' + KEY + ' ']) == ((KEY + '\n') * 2).encode()


def test_ports_are_assigned_from_the_rent_range_lowest_free_first_22_first():
    assert rt.assign_ports([8888, 22, 8888], [31000, 31099], taken=()) == {'22': 31000, '8888': 31001}
    assert rt.assign_ports([22], [31000, 31099], taken={31000, 31001}) == {'22': 31002}
    with pytest.raises(rt.RentalError, match='no free port'):
        rt.assign_ports([22, 80], [31000, 31000], taken=())
    with pytest.raises(rt.RentalError, match='no rent range'):
        rt.assign_ports([22], [], taken=())


# -- the pass ----------------------------------------------------------------------------------------------------------------


def order(
    store: rt.RentalStore,
    gpu_count: int = 2,
    hours: float = 1.0,
    image: str = 'ubuntu:24.04',
    box_uid: int | None = None,
) -> rt.RentalRecord:
    return rt.place_order(
        store,
        gpu_type='RTX5090',
        gpu_count=gpu_count,
        image=image,
        ssh_pubkeys=[KEY],
        hours=hours,
        box_uid=box_uid,
        now=NOW,
    )


def test_an_order_is_placed_started_and_active_and_the_cards_are_leased(tmp_path, boxes):
    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    r = order(store)
    assert r.state == rt.REQUESTED and r.ports == [22] and r.ends_at == NOW + 3600
    report = rec.run_pass()
    r = store.rentals[r.id]
    assert [a.kind for a in report.actions] == ['place', 'active'] and report.ok
    assert r.state == rt.ACTIVE and r.box == HK and r.box_uid == 45 and r.uuids == [U1, U2] and r.uuid == U1
    assert r.container_id == CID and r.host == '203.0.113.7' and r.port_map == {'22': 31000}
    assert r.started_at == r.pay_from == clock.t and r.pay_open
    box = boxes.boxes[HK]
    assert all(c.state == LEASED and c.instance_id == r.id for c in box.cards.values())
    # the box saw: network, firewall, image check, pull, run, keys, running check(s)
    joined = '\n'.join(runner.calls)
    assert 'docker network create' in joined and 'iptables -w -I DOCKER-USER' in joined
    assert 'docker pull -q ubuntu:24.04' in joined and '--runtime=sysbox-runc' in joined
    assert runner.stdins[rt.authorized_keys_command(CID, [KEY])] == (KEY + '\n').encode()
    # and the file on disk reads back
    again = rt.RentalStore(tmp_path / 'rentals.json')
    assert again.rentals[r.id].state == rt.ACTIVE and again.held_cards(HK) == {U1, U2}


def test_a_pod_whose_sshd_never_answers_is_a_failed_start_and_frees_the_cards(tmp_path, boxes):
    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock, probe=lambda h, p: False)
    r = order(store)
    report = rec.run_pass()
    r = store.rentals[r.id]
    assert r.state == rt.FAILED and r.reason == rt.START_FAILED and r.pay_from is None
    assert [a.kind for a in report.actions] == ['place', 'failed'] and 'no SSH banner' in report.actions[1].detail
    assert clock.t >= NOW + rt.SSHD_PROBE_TIMEOUT_S
    assert any(c.startswith('docker stop') for c in runner.calls)  # the pod is removed
    box = boxes.boxes[HK]
    assert all(c.state == CHECKING for c in box.cards.values())  # re-proved next round
    assert box.standing_events[-1]['kind'] == 'start_failed' and box.status == IDLE


def test_a_pull_that_fails_is_pull_failed(tmp_path, boxes):
    runner, clock = pod_runner(), Clock()
    runner.on(regex(r'^docker pull'), CommandResult(1, '', 'manifest unknown'))
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    r = order(store, image='nobody/nothing:latest')
    rec.run_pass()
    assert store.rentals[r.id].state == rt.FAILED and store.rentals[r.id].reason == rt.PULL_FAILED
    assert not any(c.startswith('docker run') for c in runner.calls)


def test_the_rental_ends_at_ends_at_and_the_box_earns_a_clean_lease(tmp_path, boxes):
    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    r = order(store, hours=0.5)
    rec.run_pass()
    clock.t = NOW + 1000
    rec.run_pass()  # confirmed running: the pay span moves on
    r = store.rentals[r.id]
    assert r.state == rt.ACTIVE and r.pay_through == NOW + 1000 and r.misses == 0
    clock.t = NOW + 1900
    report = rec.run_pass()
    r = store.rentals[r.id]
    assert [a.kind for a in report.actions] == ['ending', 'ended']
    assert r.state == rt.ENDED and r.reason == 'ends_at' and r.ended_at == r.stopped_at == r.pay_through == NOW + 1900
    assert not r.pay_open and not r.open
    assert any(c.startswith(f'docker stop --time {rt.STOP_GRACE_S} {CID}') for c in runner.calls)
    box = boxes.boxes[HK]
    assert all(c.state == CHECKING for c in box.cards.values())
    event = box.standing_events[-1]
    assert event['kind'] == CLEAN_LEASE and event['leased_s'] == pytest.approx(1900 - (NOW + 0 - NOW), abs=1)
    assert store.held_cards(HK) == set()


def test_an_end_order_drains_the_pod_and_a_withdrawn_order_never_places(tmp_path, boxes):
    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    r = order(store)
    rec.run_pass()
    rt.order_end(store, r.id)  # the app's `ending`, a customer stop
    rec.run_pass()
    assert store.rentals[r.id].state == rt.ENDED and store.rentals[r.id].reason == 'customer_stop'
    # a second order: nothing idle yet (cards CHECKING), so it waits; withdrawn, it ends without a placement
    r2 = order(store)
    rec.run_pass()
    assert store.rentals[r2.id].state == rt.REQUESTED
    rt.order_end(store, r2.id)
    rec.run_pass()
    assert store.rentals[r2.id].state == rt.ENDED and store.rentals[r2.id].box == ''


def test_a_pod_gone_while_the_agent_answers_is_a_heartbeat_failure(tmp_path, boxes):
    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    r = order(store)
    rec.run_pass()
    runner.on(regex(r"^docker inspect --format '\{\{\.State\.Running\}\}'"), 'false\n')
    clock.t = NOW + 600
    report = rec.run_pass()
    r = store.rentals[r.id]
    assert [a.kind for a in report.actions] == ['lost'] and r.state == rt.FAILED and r.reason == rt.BOX_LOST
    assert r.ended_at == r.stopped_at == NOW  # the lease ended at the last time we saw the pod
    box = boxes.boxes[HK]
    assert box.status == BENCHED and box.withheld_from == NOW + 600  # 23 §4a: bench, pay withheld
    assert box.standing_events[-1]['kind'] == 'heartbeat_failed'


def test_a_box_unreachable_three_passes_ends_the_rental_as_a_stop_not_a_cheat(tmp_path, boxes):
    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    r = order(store)
    rec.run_pass()
    from gittensor.controller.ssh import SshTransportError

    runner.on(regex(r"^docker inspect --format '\{\{\.State\.Running\}\}'"), SshTransportError('timed out'))
    for i in range(1, 4):
        clock.t = NOW + 60 * i
        report = rec.run_pass()
        assert report.actions[0].kind == 'miss' and store.rentals[r.id].misses == i
        assert not store.rentals[r.id].pay_open  # not paid for time we could not see
    r = store.rentals[r.id]
    assert r.state == rt.FAILED and r.reason == rt.BOX_LOST and report.actions[-1].kind == 'lost'
    box = boxes.boxes[HK]
    assert box.status == IDLE and box.withheld_from is None  # Kimbo 9/16: nothing withheld, no bench
    assert box.standing_events[-1]['kind'] == 'instance_stopped'
    assert all(c.state == CHECKING for c in box.cards.values())


def test_an_order_nothing_fits_waits_the_grace_then_fails(tmp_path, boxes):
    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    r = order(store, gpu_count=1)  # the box is a 2-card box
    assert rec.run_pass().actions == [] and store.rentals[r.id].state == rt.REQUESTED
    clock.t = NOW + rt.NO_FIT_GRACE_S
    report = rec.run_pass()
    assert store.rentals[r.id].state == rt.FAILED and store.rentals[r.id].reason == rt.NO_BOX_FITS
    assert report.actions[0].kind == 'failed'


def test_only_a_rentable_wholly_idle_box_of_the_type_and_size_is_picked(tmp_path, boxes):
    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    probation = rentable_box(standing_s=0)  # no clean lease-hours: probation, not rentable (29 §1 #7)
    boxes.put(probation)
    assert rec._pick(order(store), set(), NOW) is None
    boxes.put(rentable_box(rent_ports=[]))  # not started with --rent
    assert rec._pick(order(store), set(), NOW) is None
    boxes.put(rentable_box())
    busy = rentable_box()
    busy.cards[U1] = CardState(CHECKING, '', NOW)
    boxes.put(busy)
    assert rec._pick(order(store), set(), NOW) is None
    boxes.put(rentable_box())
    best = rentable_box(hk='5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty', standing_s=48 * 3600, uid=46)  # trusted
    boxes.put(best)
    picked = rec._pick(order(store), set(), NOW)
    assert picked is not None and picked.box_id == best.box_id
    pinned = rec._pick(order(store, box_uid=45), set(), NOW)
    assert pinned is not None and pinned.box_id == HK  # a pinned box


# -- what the rest of the controller does with a rental --------------------------------------------------------------------


def test_the_ledger_pays_a_rental_like_an_instance(tmp_path, boxes):
    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    r = order(store)
    rec.run_pass()
    clock.t = NOW + 600
    rec.run_pass()
    rows = accrue([boxes.boxes[HK]], store.rentals, Cursors(settled_at=NOW), NOW + 600)
    assert {row.uuid: row.leased_s for row in rows} == {U1: 600.0, U2: 600.0}
    assert all(row.state == LEASED and row.instance == r.id and row.idle_s == 0.0 for row in rows)


def test_the_placement_reconciler_leaves_a_rentals_cards_alone(tmp_path, boxes):
    from gittensor.controller.reconcile import InstanceStore, Reconciler
    from gittensor.controller.registry import DeploymentStore, Registry

    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    r = order(store)
    rec.run_pass()
    (tmp_path / 'registry').mkdir()
    list_runner = FakeRunner().on(regex(r'^docker ps'), '')
    placement = Reconciler(
        boxes,
        InstanceStore(tmp_path / 'instances.json'),
        DeploymentStore(tmp_path / 'deployments.json'),
        Registry(tmp_path / 'registry'),
        lambda box: list_runner,
        wall=clock,
        held_cards=store.held_cards,
    )
    report = placement.run_pass()
    assert report.actions == [] and report.ok
    assert all(c.state == LEASED and c.instance_id == r.id for c in boxes.boxes[HK].cards.values())


def test_the_public_document_marks_a_rented_card_and_nothing_else(tmp_path, boxes):
    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    r = order(store)
    rec.run_pass()
    doc = build_fleet(tmp_path, boxes.boxes, {}, {}, True, NOW + 1, rentals=store.rentals)
    row = doc['boxes'][0]
    assert row['rentable'] is True and doc['offers'] == {}  # rentable, but rented: not on offer
    assert all(c['state'] == LEASED and c['rental'] is True and 'workload' not in c for c in row['cards'])
    text = json.dumps(doc)
    for private in (r.id, CID, KEY, '31000', '203.0.113.7', 'ubuntu:24.04'):
        assert private not in text, private
    assert not re.search(r'GPU-[0-9a-f-]{20,}', text)


def test_an_interrupted_start_is_failed_on_the_next_pass(tmp_path, boxes):
    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    r = order(store)
    r.state, r.box, r.uuids, r.uuid, r.container_id = rt.STARTING_R, HK, [U1, U2], U1, CID
    store.put(r)
    box = boxes.boxes[HK]
    box.cards = {u: CardState(STARTING, r.id, NOW) for u in (U1, U2)}
    boxes.put(box)
    report = rec.run_pass()
    assert report.actions[0].kind == 'failed' and 'interrupted' in report.actions[0].detail
    assert store.rentals[r.id].state == rt.FAILED
    assert all(c.state == CHECKING for c in boxes.boxes[HK].cards.values())


def test_the_dev_overrides_run_a_pod_under_runc_without_the_firewall_on_a_probation_box(tmp_path, boxes):
    """Our own test boxes (a Lium pod cannot run Sysbox, has no reachable host namespaces, and is on probation): the
    reconciler takes the overrides; a miner's box never gets them (the CLI warns)."""
    runner, clock = pod_runner(), Clock()
    boxes.put(rentable_box(standing_s=0))  # probation
    store, rec = reconciler(tmp_path, boxes, runner, clock, runtime='runc', firewall=False, min_standing='probation')
    r = order(store)
    rec.run_pass()
    assert store.rentals[r.id].state == rt.ACTIVE
    run_line = next(c for c in runner.calls if c.startswith('docker run'))
    assert '--runtime=runc' in run_line and not any('iptables' in c for c in runner.calls)
    assert rt.pod_run_command(rt.RentalRecord('rnt_1', uuids=[U1], image='x')).count('--runtime=sysbox-runc') == 1
