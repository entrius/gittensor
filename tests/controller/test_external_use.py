# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The lease accounting check in the watch, over the fake box and a fake gateway: honest traffic stays clear; the
runtime's counters running ahead of the gateway twice in a row end the lease through the planned drain (not a bench),
with one SOFT ``external_use`` event carrying the reason and the numbers; the box takes no new lease for the cooldown,
then does; the third inside a week benches it; a runtime with no counters table, a ``/metrics`` that does not answer
and a gateway that cannot be read are no judgement, logged once; a restarted controller starts a new baseline; the
throughput evidence never acts; the public fleet document carries the words, never the numbers; the operator log
carries every number."""

import json

import pytest

import gittensor.cli.main  # noqa: F401  (the CLI package must load before gittensor.controller.cli)
from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.runner import CommandResult
from gittensor.controller.checks.state import (
    BENCHED,
    CHECKING,
    CLEAN_LEASE,
    EXTERNAL_USE,
    IDLE,
    LEASED,
)
from gittensor.controller.cli import _DaemonPrinter
from gittensor.controller.heartbeat import Watch
from gittensor.controller.publish import build_fleet
from gittensor.controller.registry import Registry, make_entry, sign_bytes
from tests.controller.conftest import NVML_MD5, UUID_5090, UUID_5090_B
from tests.controller.test_placement import (
    Clock,
    FakeDocker,
    idle_box,
    keypair,
    placeholder_doc,
    reconciler,
    seed,
)

HOTKEY = '5' + 'F' * 47  # a box id the public document publishes


class MeteredBox(FakeDocker):
    """A fake box whose workload serves Prometheus counters at ``/metrics``."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.completion, self.requests = 0, 0
        self.metrics_status = 200

    def respond(self, command):
        if command.startswith('curl ') and command.endswith('/metrics'):
            if self.metrics_status != 200:
                return CommandResult(0, f'not found\n{self.metrics_status}')
            body = (
                '# TYPE sparkinfer_tokens_total counter\n'
                f'sparkinfer_tokens_total{{kind="completion",model="m"}} {self.completion}\n'
                f'sparkinfer_tokens_total{{kind="prompt",model="m"}} {self.completion * 10}\n'
                f'sparkinfer_requests_total {self.requests}\nsparkinfer_active_requests 0\n'
            )
            return CommandResult(0, body + '\n200')
        return super().respond(command)


class FakeGateway:
    """The gateway's ``/healthz`` as the controller reads it."""

    def __init__(self):
        self.started_at = 500.0
        self.served: dict[str, dict] = {}
        self.in_flight: dict[str, int] = {}
        self.up = True
        self.reads = 0

    def send(self, instance, tokens=0, allowance=0):
        row = self.served.setdefault(
            instance, {'requests': 0, 'completion_tokens': 0, 'unaccounted_requests': 0, 'unaccounted_allowance_tokens': 0}
        )  # fmt: skip
        row['requests'] += 1
        row['completion_tokens'] += tokens
        if allowance:
            row['unaccounted_requests'] += 1
            row['unaccounted_allowance_tokens'] += allowance

    def __call__(self):
        self.reads += 1
        if not self.up:
            return None
        doc = {'status': 'ok', 'in_flight': dict(self.in_flight), 'refreshed_at': 1.0}
        return {**doc, 'started_at': self.started_at, 'served': json.loads(json.dumps(self.served))}


def make_world(tmp_path, **profile):
    key, pub = keypair(tmp_path)
    registry = Registry(tmp_path / 'registry', pub)
    doc = placeholder_doc()
    if profile:
        doc['profile'] = profile
    verified = make_entry(doc, now=1.0)
    registry.write(verified, sign_bytes(verified.entry.canonical_bytes(), key))
    return tmp_path, registry


@pytest.fixture
def counters(monkeypatch):
    """The placeholder manifest's runtime ('custom') read with sparkinfer's counters table."""
    monkeypatch.setitem(cfg.RUNTIME_COUNTERS, 'custom', cfg.RUNTIME_COUNTERS['sparkinfer'])


def lease(tmp_path, gateway=True, events=(), **profile):
    """One replica LEASED on UUID_5090 of a two-card box, a watch with the gateway (or none) to ask."""
    root, registry = make_world(tmp_path, **profile)
    box_state = idle_box(HOTKEY)
    box_state.identity = {'power_limits': {UUID_5090: 575.0, UUID_5090_B: 575.0}, 'nvml_md5': NVML_MD5}
    box_state.standing_events = list(events)
    seed(root, box_state, replicas=1)
    box = MeteredBox()
    clock = Clock()
    clock.t = 1_000_000.0
    rec = reconciler(root, registry, {HOTKEY: box}, clock=clock)
    assert rec.run_pass().ok
    gw = FakeGateway()
    watch = Watch(
        rec.boxes, rec.instances, registry, make_runner=lambda b: box.runner, clock=clock, wall=clock, lock=rec._lock,
        gateway_state=gw if gateway else None,
    )  # fmt: skip
    (record,) = rec.instances.instances.values()
    return rec, watch, box, gw, clock, record


def visit(watch, clock, seconds=60.0):
    clock.t += seconds
    return watch.run_pass()


def kinds(report):
    return [row['kind'] for row in report.usage]


def detect(watch, box, clock, record_id):
    """Two visits in a row on which the runtime made 5000 tokens the gateway never sent."""
    box.completion += 5000
    first = visit(watch, clock)
    box.completion += 5000
    second = visit(watch, clock)
    assert kinds(first) == ['strike'] and kinds(second) == ['detection']
    return second


def test_honest_traffic_stays_clear_and_the_probes_count_no_tokens(tmp_path, counters):
    rec, watch, box, gw, clock, record = lease(tmp_path)
    box.completion = 64  # the start's canary, before the lease
    report = watch.run_pass()
    assert kinds(report) == ['baseline'] and report.usage[0]['runtime_completion_tokens'] == 64
    for _ in range(5):
        box.completion += 3000
        gw.send(record.id, 3000)
        box.requests += 25  # keeper and health probes: requests, no tokens
        report = visit(watch, clock)
        assert kinds(report) == ['clear'] and report.usage[0]['surplus'] == 0
    assert gw.reads == 12  # before and after the runtime, every visit
    assert not rec.instances.instances[record.id].draining
    assert not [e for e in rec.boxes.boxes[HOTKEY].standing_events if e['kind'] == EXTERNAL_USE]


def test_two_strikes_end_the_lease_through_the_planned_drain_not_a_bench(tmp_path, counters):
    rec, watch, box, gw, clock, record = lease(tmp_path)
    watch.run_pass()
    report = detect(watch, box, clock, record.id)
    (action,) = [a for a in report.actions if a.kind == 'external_use']
    assert action.detail.startswith('suspected external (non-gateway) usage:') and 'surplus 10000' in action.detail
    current = rec.instances.instances[record.id]
    assert current.draining and not current.healthy and not current.pay_open
    assert current.stopped_at == clock.t and current.ended_by == EXTERNAL_USE  # pay runs to the detection
    box_state = rec.boxes.boxes[HOTKEY]
    assert box_state.status == IDLE and box_state.withheld_from is None and box_state.bench_count == 0
    assert box_state.cards[UUID_5090].state == LEASED  # the reconciler's drain moves it
    event = box_state.standing_events[-1]
    assert event['kind'] == EXTERNAL_USE and event['reason'] == 'suspected external (non-gateway) usage'
    assert (event['instance'], event['uuid'], event['surplus'], event['runtime_delta']) == (record.id, UUID_5090, 10_000, 10_000)  # fmt: skip
    assert visit(watch, clock).usage == []  # no longer watched

    drained = rec.run_pass()
    (drain,) = [a for a in drained.actions if a.kind == 'drain']
    assert drain.states == [LEASED, 'DRAINING', CHECKING] and 'grace' in drain.detail  # the planned drain's wait
    assert box.commands('docker stop --time 5')  # the manifest's drain, not a kill
    assert record.id not in rec.instances.instances
    assert not [e for e in rec.boxes.boxes[HOTKEY].standing_events if e['kind'] == CLEAN_LEASE]  # no clean lease-hours


def test_the_box_takes_no_new_lease_for_the_cooldown_then_does(tmp_path, counters):
    rec, watch, box, gw, clock, record = lease(tmp_path)
    watch.run_pass()
    detect(watch, box, clock, record.id)
    report = rec.run_pass()  # drains; the other card is IDLE but the box is cooling down
    assert [a.kind for a in report.actions] == ['drain']
    assert any('1 replica(s) short' in e for e in report.errors)
    assert rec.boxes.boxes[HOTKEY].cards[UUID_5090_B].state == IDLE
    clock.t += cfg.EXTERNAL_USE_COOLDOWN_S - 120
    assert [a.kind for a in rec.run_pass().actions] == []
    clock.t += 120
    (start,) = rec.run_pass().actions
    assert start.kind == 'start' and start.ok and start.uuid == UUID_5090_B


def test_the_third_inside_a_week_benches_the_box_at_the_16_hour_rung(tmp_path, counters):
    day = 86_400.0
    earlier = [
        {'at': 1_000_000.0 - 5 * day, 'kind': EXTERNAL_USE, 'uuid': UUID_5090, 'reason': cfg.EXTERNAL_USE_REASON},
        {'at': 1_000_000.0 - 1 * day, 'kind': EXTERNAL_USE, 'uuid': UUID_5090, 'reason': cfg.EXTERNAL_USE_REASON},
    ]
    rec, watch, box, gw, clock, record = lease(tmp_path, events=earlier)
    watch.run_pass()
    report = detect(watch, box, clock, record.id)
    (action,) = [a for a in report.actions if a.kind == 'external_use']
    assert action.states == [LEASED, BENCHED]
    box_state = rec.boxes.boxes[HOTKEY]
    assert box_state.status == BENCHED and box_state.last_failed == [EXTERNAL_USE]
    assert box_state.bench_until == clock.t + 57_600 and box_state.withheld_from is None
    assert all(r.draining for r in rec.instances.on_box(HOTKEY))
    drained = rec.run_pass()
    assert [a.kind for a in drained.actions] == ['drain'] and not rec.instances.on_box(HOTKEY)


@pytest.mark.parametrize(
    'setup, why',
    [
        (lambda box, gw, mp: None, "runtime 'custom' has no counters table"),
        (lambda box, gw, mp: (mp(), setattr(box, 'metrics_status', 404)), '/metrics -> 404'),
        (lambda box, gw, mp: (mp(), setattr(gw, 'up', False)), 'gateway /healthz not read, or without totals'),
    ],
)
def test_nothing_readable_is_no_judgement_logged_once(tmp_path, monkeypatch, setup, why):
    rec, watch, box, gw, clock, record = lease(tmp_path)
    setup(box, gw, lambda: monkeypatch.setitem(cfg.RUNTIME_COUNTERS, 'custom', cfg.RUNTIME_COUNTERS['sparkinfer']))
    first = watch.run_pass()
    assert kinds(first) == ['no_judgement'] and first.usage[0]['detail'] == why
    for _ in range(3):
        box.completion += 50_000  # whatever the runtime does, nothing can be judged
        assert visit(watch, clock).usage == []
    assert not rec.instances.instances[record.id].draining


def test_no_gateway_to_ask_is_no_judgement(tmp_path, counters):
    rec, watch, box, gw, clock, record = lease(tmp_path, gateway=False)
    first = watch.run_pass()
    assert kinds(first) == ['no_judgement'] and first.usage[0]['detail'] == 'no gateway to ask'
    box.completion += 50_000
    assert visit(watch, clock).usage == [] and not rec.instances.instances[record.id].draining


def test_a_restarted_controller_or_gateway_starts_a_new_baseline(tmp_path, counters):
    rec, watch, box, gw, clock, record = lease(tmp_path)
    watch.run_pass()
    box.completion += 5000
    assert kinds(visit(watch, clock)) == ['strike']
    fresh = Watch(
        rec.boxes, rec.instances, watch.registry, watch.make_runner, clock=clock, wall=clock, lock=rec._lock,
        gateway_state=gw,
    )  # fmt: skip
    box.completion += 5000
    assert kinds(visit(fresh, clock)) == ['baseline']  # its baseline is lost with the process: never a strike
    gw.started_at, gw.served = 900.0, {}
    box.completion += 5000
    assert kinds(visit(fresh, clock)) == ['rebaseline']
    assert kinds(visit(fresh, clock)) == ['clear']


def test_throughput_evidence_is_logged_and_never_acts(tmp_path, counters):
    rec, watch, box, gw, clock, record = lease(tmp_path, decode_tps_single=92)
    watch.run_pass()
    gw.send(record.id, 3000)
    gw.served[record.id].update(decode_tps_alone_p50=30.0, decode_tps_alone_n=25)
    box.completion += 3000
    report = visit(watch, clock)
    assert kinds(report) == ['clear', 'throughput_low']
    row = report.usage[1]
    assert (row['decode_tps_alone_p50'], row['decode_tps_single'], row['decode_tps_alone_n']) == (30.0, 92.0, 25)
    assert [a.kind for a in report.actions] == ['heartbeat', 'health']  # nothing else
    assert not rec.instances.instances[record.id].draining
    assert [e['kind'] for e in rec.boxes.boxes[HOTKEY].standing_events] == []


def test_the_fleet_document_carries_the_words_never_the_numbers(tmp_path, counters):
    rec, watch, box, gw, clock, record = lease(tmp_path)
    watch.run_pass()
    detect(watch, box, clock, record.id)
    doc = build_fleet(tmp_path, rec.boxes.boxes, rec.instances.instances, {}, True, clock.t)
    (row,) = doc['boxes']
    assert row['last_event'] == {'at': clock.t, 'kind': EXTERNAL_USE}
    text = json.dumps(doc)
    for private in ('surplus', 'runtime_delta', 'threshold', 'suspected', 'gateway_delta', 'in_flight', 'ceiling'):
        assert private not in text, private

    day = 86_400.0
    events = [{'at': clock.t - day, 'kind': EXTERNAL_USE}, {'at': clock.t - 2 * day, 'kind': EXTERNAL_USE}]
    (tmp_path / 'b').mkdir()
    rec2, watch2, box2, _, clock2, record2 = lease(tmp_path / 'b', events=events)
    watch2.run_pass()
    detect(watch2, box2, clock2, record2.id)
    (benched,) = build_fleet(tmp_path, rec2.boxes.boxes, rec2.instances.instances, {}, True, clock2.t)['boxes']
    assert benched['status'] == BENCHED and benched['last_failed'] == [EXTERNAL_USE]
    assert benched['benched_reason'] == EXTERNAL_USE and 'surplus' not in json.dumps(benched)


def test_the_operator_log_carries_every_number(tmp_path, counters, capsys):
    rec, watch, box, gw, clock, record = lease(tmp_path)
    watch.run_pass()
    report = detect(watch, box, clock, record.id)
    _DaemonPrinter(json_mode=True).watch(report)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    (row,) = [line for line in lines if line['event'] == 'usage_check']
    assert row['kind'] == 'detection' and row['instance'] == record.id and row['box'] == HOTKEY
    for key in ('runtime_delta', 'gateway_delta', 'unaccounted_allowance_delta', 'in_flight', 'surplus', 'threshold'):
        assert key in row, key
    assert [line['kind'] for line in lines if line['event'] == 'watch'][-1] == 'external_use'
