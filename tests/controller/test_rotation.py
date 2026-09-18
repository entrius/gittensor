# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Rotation and standing-ordered placement over the fake box (vault ``23`` §8, ``24`` §3 WS-E): a lease past its cap is
replaced first and drained only once the replacement is LEASED; at most ~10% of leased cards cycle at once; no free card
means the lease is cycled in place within that same budget; a failed replacement calls the rotation off; placement prefers the
best standing, then the freshest check, and a release takes the lowest standing first. A normal drain records the
lease's clean seconds."""

import pytest

from gittensor.controller.checks.state import CHECKING, IDLE, LEASED, START_FAILED, apply_verdict
from gittensor.controller.registry import DeploymentStore
from gittensor.controller.standing import CLEAN_LEASE
from tests.controller.conftest import UUID_5090, UUID_5090_B
from tests.controller.test_placement import ENTRY, Clock, FakeDocker, idle_box, make_world, reconciler, seed
from tests.controller.test_state import admit_verdict

H = 3_600.0


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def records(rec):
    return sorted(rec.instances.instances.values(), key=lambda r: r.leased_at or 0.0)


def test_a_lease_past_its_cap_is_replaced_first_and_drained_after(world):
    root, registry = world
    seed(root, idle_box('hk1'), replicas=1)
    clock = Clock()
    rec = reconciler(root, registry, {'hk1': FakeDocker()}, clock=clock)
    assert rec.run_pass().ok
    (old,) = records(rec)
    assert 0.4 * H <= old.lease_cap_s <= 0.6 * H  # a new box is on probation: a short lease
    assert old.pay_open and old.pay_from == old.pay_through == old.leased_at

    clock.t += old.lease_cap_s - 1
    assert rec.run_pass().rotations == []  # not yet

    clock.t += 2
    report = rec.run_pass()
    assert report.ok and report.rotations == [old.id]
    old_now, new = records(rec)
    assert old_now.id == old.id and old_now.rotating == 'hk1' and not old_now.draining  # still serving
    assert new.replaces == old.id and rec.boxes.boxes['hk1'].cards[new.uuid].state == LEASED
    assert report.running == {ENTRY: 2}  # replacement first: never below the replica count while it loads

    report = rec.run_pass()
    assert [a.kind for a in report.actions] == ['drain'] and report.actions[0].instance == old.id
    assert list(rec.instances.instances) == [new.id]
    box = rec.boxes.boxes['hk1']
    assert box.cards[old.uuid].state == CHECKING and box.cards[new.uuid].state == LEASED
    (event,) = [e for e in box.standing_events if e['kind'] == CLEAN_LEASE]
    assert event['instance'] == old.id and event['leased_s'] == pytest.approx(old.lease_cap_s + 1, abs=1)


def test_at_most_a_tenth_of_leased_cards_cycle_at_once(world):
    root, registry = world
    ids = [f'hk{i:02d}' for i in range(11)]
    seed(root, *(idle_box(i, host=f'10.0.0.{n}') for n, i in enumerate(ids)), replicas=20)
    clock = Clock()
    rec = reconciler(root, registry, {i: FakeDocker() for i in ids}, clock=clock)
    assert rec.run_pass().ok and len(rec.instances.instances) == 20  # 22 cards, 2 left free

    clock.t += 3 * H  # every lease is past its cap
    report = rec.run_pass()
    assert len(report.rotations) == 2  # 10% of 20
    assert sum(1 for r in rec.instances.instances.values() if r.rotating) == 2

    report = rec.run_pass()
    assert [a.kind for a in report.actions] == ['drain', 'drain'] and report.rotations == []  # freed cards are CHECKING
    assert len(rec.instances.instances) == 20


def proved(rec, box_id, uuid, at):
    """What the re-prove does to a drained card: a passing proof returns it from CHECKING to IDLE."""
    box = rec.boxes.boxes[box_id]
    rec.boxes.put(apply_verdict(box, admit_verdict(box.pinned_uuids), at, proved=[uuid]))


def test_no_free_card_cycles_the_lease_in_place(world):
    """A fleet leased to capacity (Kimbo 9/18): no card to rotate onto, so the lease is drained where it is, its card
    is re-proved, and the replica starts again; the drain is what writes the clean lease-hours."""
    root, registry = world
    seed(root, idle_box('hk1', uuids=(UUID_5090,)), replicas=1)
    clock = Clock()
    rec = reconciler(root, registry, {'hk1': FakeDocker(gpus=(UUID_5090,))}, clock=clock)
    assert rec.run_pass().ok
    (old,) = records(rec)

    clock.t += old.lease_cap_s - 1
    report = rec.run_pass()
    assert report.rotations == [] and report.actions == []  # inside its cap the lease runs on

    clock.t += 2
    report = rec.run_pass()
    assert report.rotations == [old.id] and [a.kind for a in report.actions] == ['drain']
    box = rec.boxes.boxes['hk1']
    assert box.cards[UUID_5090].state == CHECKING and not rec.instances.instances
    (event,) = [e for e in box.standing_events if e['kind'] == CLEAN_LEASE]
    assert event['leased_s'] == pytest.approx(old.lease_cap_s + 1, abs=1)

    assert rec.run_pass().actions == []  # not proved yet: nothing starts on a CHECKING card
    proved(rec, 'hk1', UUID_5090, clock.t)
    report = rec.run_pass()
    (new,) = records(rec)
    assert [a.kind for a in report.actions] == ['start'] and new.id != old.id and not new.replaces
    assert rec.boxes.boxes['hk1'].cards[UUID_5090].state == LEASED


def test_in_place_cycling_keeps_to_the_rotation_budget(world):
    """Two cards, two replicas, both past their cap: one is cycled, the other waits until the first is LEASED again."""
    root, registry = world
    seed(root, idle_box('hk1'), replicas=2)
    clock = Clock()
    rec = reconciler(root, registry, {'hk1': FakeDocker()}, clock=clock)
    assert rec.run_pass().ok and len(rec.instances.instances) == 2

    clock.t += 3 * H
    report = rec.run_pass()
    assert len(report.rotations) == 1 and [a.kind for a in report.actions] == ['drain']
    (first,) = report.rotations
    (other,) = records(rec)

    report = rec.run_pass()  # the drained card is CHECKING: it counts against the budget, the other lease runs on
    assert report.rotations == [] and report.actions == []

    freed = next(u for u, c in rec.boxes.boxes['hk1'].cards.items() if c.state == CHECKING)
    proved(rec, 'hk1', freed, clock.t)
    report = rec.run_pass()  # the replica comes back first; the other lease is still not touched in this pass
    assert [a.kind for a in report.actions] == ['start'] and report.rotations == []
    assert other.id in rec.instances.instances and first not in rec.instances.instances

    report = rec.run_pass()  # now everything is LEASED again: the second lease takes its turn
    assert report.rotations == [other.id]


def test_a_failed_replacement_calls_the_rotation_off(world):
    root, registry = world
    seed(root, idle_box('hk1'), replicas=1)
    clock = Clock()
    box = FakeDocker()
    rec = reconciler(root, registry, {'hk1': box}, clock=clock)
    assert rec.run_pass().ok
    (old,) = records(rec)
    clock.t += H
    box.healthy = False  # the replacement never becomes healthy by max_load_s
    report = rec.run_pass()
    assert report.rotations == [old.id] and not report.ok
    state = rec.boxes.boxes['hk1']
    assert state.standing_events[-1]['kind'] == START_FAILED and state.cards[UUID_5090_B].state == CHECKING

    box.healthy = True
    report = rec.run_pass()
    (record,) = records(rec)
    assert record.id == old.id and record.rotating == '' and not record.draining  # called off, still leased
    assert report.rotations == []  # the other card is CHECKING: nothing to rotate onto yet


def test_placement_prefers_standing_then_freshness_and_releases_the_lowest_standing_first(world):
    root, registry = world
    trusted = idle_box('trusted', uuids=(UUID_5090,), host='10.0.0.1')
    trusted.last_check_at = 50.0  # an older check than the probation box
    trusted.standing_events = [{'at': 1.0, 'kind': CLEAN_LEASE, 'leased_s': 60 * H}]
    fresh = idle_box('fresh', uuids=(UUID_5090,), host='10.0.0.2')
    fresh.last_check_at = 900.0
    seed(root, trusted, fresh, replicas=1)
    boxes = {'trusted': FakeDocker(gpus=(UUID_5090,)), 'fresh': FakeDocker(gpus=(UUID_5090,))}
    clock = Clock()
    rec = reconciler(root, registry, boxes, clock=clock)
    assert rec.run_pass().ok
    (first,) = records(rec)
    assert first.box == 'trusted' and 1.6 * H <= first.lease_cap_s <= 2.4 * H  # trusted: first pick, longer lease

    DeploymentStore(root / 'deployments.json').set(ENTRY, True, 2)
    rec.deployments = DeploymentStore(root / 'deployments.json')
    assert rec.run_pass().ok and len(rec.instances.instances) == 2
    DeploymentStore(root / 'deployments.json').set(ENTRY, True, 1)
    rec.deployments = DeploymentStore(root / 'deployments.json')
    report = rec.run_pass()
    assert [(a.kind, a.box) for a in report.actions] == [('drain', 'fresh')]  # probation goes before trusted
    assert rec.boxes.boxes['trusted'].cards[UUID_5090].state == LEASED
    assert rec.boxes.boxes['fresh'].status == IDLE
