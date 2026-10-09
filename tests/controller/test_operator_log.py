"""The operator's log (``gitt controller run --json``): what a person reading controller.log after a miner's or a
customer's bug report must find there (vault 33 §10 review, 10/9)."""

from __future__ import annotations

import json
import re

import pytest

import gittensor.cli.main  # noqa: F401  (the CLI package must load before gittensor.controller.cli)
import gittensor.controller.rentals as rt
from gittensor.controller.checks.state import StateStore
from gittensor.controller.checks.verdict import CheckResult, CheckVerdict
from gittensor.controller.cli import _DaemonPrinter
from gittensor.controller.daemon import _where
from tests.controller.test_rentals import HK, Clock, order, pod_runner, reconciler, rentable_box

NOW = 1_791_500_000.0


@pytest.fixture
def boxes(tmp_path) -> StateStore:
    store = StateStore(tmp_path / 'boxes.json')
    store.put(rentable_box())
    return store


def _lines(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_every_json_event_carries_a_readable_utc_time(capsys):
    _DaemonPrinter(json_mode=True).note('pay', 'hello')
    (line,) = _lines(capsys)
    assert line['event'] == 'note' and isinstance(line['at'], float)
    assert (
        line['at_iso'].endswith('Z') and 'T' in line['at_iso'] and len(line['at_iso']) == 24
    )  # 2026-10-09T15:25:21.155Z


def test_a_rental_action_is_one_typed_line_with_the_id_the_box_and_the_detail(capsys):
    printer = _DaemonPrinter(json_mode=True)
    printer.rental(
        rt.RentalAction('failed', 'rnt_1', HK, 'start_failed: docker run: exit 125: Unable to find group render')
    )
    printer.rental(rt.RentalAction('alive', 'rnt_1', HK, 'pod abc running'))
    bad, alive = _lines(capsys)
    assert bad['event'] == 'rental' and bad['kind'] == 'failed' and bad['rental'] == 'rnt_1' and bad['box'] == HK
    assert bad['ok'] is False and 'Unable to find group render' in bad['detail']
    assert alive['ok'] is True and alive['kind'] == 'alive'


def test_what_a_start_thread_decides_reaches_the_next_pass_and_the_log(tmp_path, boxes):
    """In the daemon the start runs in a thread after its pass was reported: `active` / `failed` would be lost."""
    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    rec.background = True  # the thread's report is the one the pass already returned
    r = order(store)
    first = rec.run_pass()  # places; the start runs in its thread after this report is returned
    assert [a.kind for a in first.actions] == ['place'] and rec.join(5.0)
    assert rec._late and rec._late[-1].kind == 'active'
    nxt = rec.run_pass()
    assert [a.kind for a in nxt.actions][0] == 'active' and nxt.actions[0].rental == r.id and not rec._late


def test_a_running_pod_says_alive_once_per_interval_not_every_pass(tmp_path, boxes):
    runner, clock = pod_runner(), Clock()
    store, rec = reconciler(tmp_path, boxes, runner, clock, alive_interval_s=60.0)
    r = order(store)
    rec.run_pass()
    kinds = [a.kind for a in rec.run_pass().actions]
    assert kinds == ['alive']  # the first confirmation after the start
    clock.t += 10
    assert [a.kind for a in rec.run_pass().actions] == []  # confirmed again, nothing said
    clock.t += 60
    (alive,) = rec.run_pass().actions
    assert alive.kind == 'alive' and alive.rental == r.id and 'running' in alive.detail
    # without an interval (one-shot CLI passes, older tests) nothing is said
    store2, rec2 = reconciler(tmp_path / 'b', boxes, runner, clock)
    assert rec2.alive_interval_s is None


def test_a_failed_check_in_the_round_carries_the_evidence_not_just_the_phrase(capsys):
    from gittensor.controller.checks.state import ADMIT, BoxState
    from gittensor.controller.checks.verdict import BENCH
    from gittensor.controller.cli import BoxRound, RoundReport

    box = BoxState(HK, status=ADMIT, pinned_uuids=[], host='203.0.113.7', port=2200)
    failed = CheckResult('gpu_spec', False, evidence={'device': '0x74b9', 'cards': 1, 'detail': 'MI325X is listed'})
    row = BoxRound(box=box, status_before=ADMIT, verdict=CheckVerdict(BENCH, [failed]))
    _DaemonPrinter(json_mode=True).round(RoundReport('gtp-dev', [row], [], {'total': 1.0}), 7)
    (line,) = _lines(capsys)
    (b,) = line['boxes']
    assert b['failed'] == ['gpu_spec'] and 'gpu_spec' in b['why']
    assert json.loads(b['evidence']['gpu_spec']) == {'cards': 1, 'detail': 'MI325X is listed', 'device': '0x74b9'}


def test_a_crashed_pass_names_its_type_message_and_line():
    def boom():
        raise KeyError('rnt_7')

    try:
        boom()
    except KeyError as e:
        text = _where(e)
    assert text.startswith("KeyError: 'rnt_7' (test_operator_log.py:") and text.endswith(' in boom)')


def test_a_box_that_stays_dark_is_said_once_per_reason_not_every_pass(capsys):
    from gittensor.controller.reconcile import ReconcileReport

    printer = _DaemonPrinter(json_mode=True)
    dark = ReconcileReport(unreachable={HK: 'SshTransportError: connection refused'})
    printer.reconcile(dark, 1)
    printer.reconcile(dark, 2)
    printer.reconcile(ReconcileReport(unreachable={HK: 'SshTransportError: timed out'}), 3)
    printer.reconcile(ReconcileReport(), 4)
    printer.reconcile(dark, 5)  # back to dark after answering: said again
    lines = [line for line in _lines(capsys) if line['event'] == 'reconcile_unreachable']
    assert [line['why'].split(': ')[1] for line in lines] == ['connection refused', 'timed out', 'connection refused']


def test_a_pre_pull_that_fails_is_said_with_dockers_words(tmp_path, boxes):
    from gittensor.controller.checks.runner import CommandResult

    runner, clock = pod_runner(), Clock()
    runner.on(re.compile(r'^docker pull'), CommandResult(1, '', 'pull access denied for nope/image'))
    store, rec = reconciler(tmp_path, boxes, runner, clock)
    rec.prepull_images, rec.background = ('nope/image:1',), True
    from gittensor.controller.checks.state import BoxState

    rec._pull_missing(boxes.boxes[HK])
    (action,) = rec._late
    assert action.kind == 'prepull_failed' and action.box == HK and 'pull access denied' in action.detail
    assert isinstance(boxes.boxes[HK], BoxState)
