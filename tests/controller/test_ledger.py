# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The pay ledger from a scripted card-state timeline (pay starts at the first probe after the canary, is confirmed by
the checks after it, stops at the last passing check on a failure and at the controller's stop; idle needs a fresh
passing proof; STARTING / CHECKING are unpaid; multi-card instances are all or nothing; the withheld window), the rows
on disk and the cursor across a restart, the pool math (below / at / above target_fleet, the pool bound, recycle,
leased > idle), and the price oracle's fail-safe."""

import json
import math

import pytest

from gittensor.controller.checks.state import CHECKING, DRAINING, IDLE, LEASED, STARTING, BoxState, CardState
from gittensor.controller.heartbeat import observe_pay
from gittensor.controller.pay.ledger import (
    DAY_S,
    Cursors,
    Ledger,
    LedgerRow,
    accrue,
    settle_window,
    withheld_window,
)
from gittensor.controller.pay.oracle import FailSafeOracle, MetagraphedOracle, OracleError, Quote, StaticOracle
from gittensor.controller.pay.rates import GpuRate
from gittensor.controller.reconcile import InstanceRecord

CARD = 'NVIDIA GeForce RTX 5090'
A, B, C, D = 'GPU-a', 'GPU-b', 'GPU-c', 'GPU-d'
PAY_FLAGS = {'we_started': True, 'blessed_digest': True, 'healthy': True, 'heartbeat': True}


def box(hotkey='hk1', cards=None, last_check_at=1_000.0, **kw):
    cards = cards or {}
    return BoxState(
        hotkey, status=IDLE, pinned_uuids=list(cards), card_name=CARD, last_check_at=last_check_at, cards=cards, **kw
    )


def leased_record(instance='i1', uuid=B, at=1_000.0, hotkey='hk1'):
    """What the reconciler records at LEASED: health passed at the first probe after the canary, the span opened."""
    return InstanceRecord(
        instance, 'e@1', hotkey, uuid, healthy=True, leased_at=at, last_health_at=at, health_ok=True,
        pay_from=at, pay_through=at, pay_open=True,
    )  # fmt: skip


def heartbeat(record, at, ok=True):
    record.last_heartbeat_at, record.heartbeat_ok = at, ok
    record.heartbeat = {'ok': ok, 'pay': {**PAY_FLAGS, 'heartbeat': ok}}
    observe_pay(record, at)


def health(record, at, ok=True):
    record.last_health_at, record.health_ok = at, ok
    observe_pay(record, at)


def by_uuid(rows):
    return {r.uuid: r for r in rows}


# ---------------------------------------------------------------- accrual --------------------------------------------


def test_a_scripted_lease_is_paid_from_the_canary_through_the_last_passing_check_and_stops_at_our_stop():
    record = leased_record()
    state = box(cards={A: CardState(IDLE, '', 1_000.0), B: CardState(LEASED, 'i1', 1_000.0)})
    instances = {'i1': record}
    cursors = Cursors(settled_at=1_000.0)
    paid = {'idle': 0.0, 'leased': 0.0}

    def tick(now):
        rows = by_uuid(accrue([state], instances, cursors, now))
        paid['idle'] += rows[A].idle_s
        paid['leased'] += rows[B].leased_s
        return rows

    assert tick(1_012.0)[B].leased_s == 0  # LEASED, but nothing confirmed by a heartbeat yet
    heartbeat(record, 1_030.0)  # confirms through the older check: the probe at 1000
    assert tick(1_036.0)[B].leased_s == 0
    health(record, 1_060.0)  # now both checks are past 1030
    assert tick(1_072.0)[B].leased_s == 30
    heartbeat(record, 1_090.0)  # through 1060
    health(record, 1_120.0, ok=False)  # a mid-lease failure: pay stops at the last passing check (1060)
    assert tick(1_132.0)[B].leased_s == 30
    heartbeat(record, 1_150.0)  # health still failing: nothing
    health(record, 1_180.0)  # all four hold again: a new span opens here, the gap is never paid
    assert tick(1_192.0)[B].leased_s == 0
    heartbeat(record, 1_210.0)
    health(record, 1_240.0)  # through 1210
    record.stopped_at, record.draining = 1_225.0, True  # our stop at 1225
    state.cards[B] = CardState(DRAINING, 'i1', 1_225.0)
    assert tick(1_252.0)[B].leased_s == 30  # 1180 -> 1210, confirmed before the stop
    record.pay_through = 1_300.0  # nothing past our stop is ever paid, whatever the record says
    assert tick(1_264.0)[B].leased_s == 15  # 1210 -> 1225
    assert paid == {'idle': 264.0, 'leased': 105.0}
    assert tick(1_276.0)[B].leased_s == 0  # each second once


def test_idle_needs_a_fresh_passing_proof_and_starting_checking_and_benched_are_unpaid():
    cards = {A: CardState(IDLE, '', 900.0), B: CardState(STARTING, 'i2', 900.0), C: CardState(CHECKING, '', 900.0)}
    starting = InstanceRecord('i2', 'e@1', 'hk1', B)  # no span: never reached LEASED
    state = box(cards=cards, last_check_at=1_000.0)
    rows = by_uuid(accrue([state], {'i2': starting}, Cursors(settled_at=1_000.0), 1_012.0))
    assert (rows[A].idle_s, rows[B].idle_s, rows[B].leased_s, rows[C].idle_s) == (12, 0, 0, 0)
    assert rows[B].state == STARTING and rows[B].instance == 'i2'

    stale = box(cards={A: CardState(IDLE, '', 0.0)}, last_check_at=0.0)  # proof 1812 s old: past 1.5 rounds
    assert accrue([stale], {}, Cursors(settled_at=1_800.0), 1_812.0)[0].idle_s == 0
    unreachable = box(cards={A: CardState(IDLE, '', 900.0)}, unreachable_count=1)
    assert accrue([unreachable], {}, Cursors(settled_at=1_000.0), 1_012.0)[0].idle_s == 0
    became_idle = box(cards={A: CardState(IDLE, '', 1_005.0)})  # a CHECKING card proved at 1005
    assert accrue([became_idle], {}, Cursors(settled_at=1_000.0), 1_012.0)[0].idle_s == 7
    benched = BoxState('hk1', status='BENCHED')
    assert accrue([benched], {}, Cursors(settled_at=1_000.0), 1_012.0) == []


def test_a_multi_card_instance_is_paid_all_or_nothing():
    record = leased_record('i3', A)
    record.pay_through = 1_060.0
    cards = {A: CardState(LEASED, 'i3', 1_000.0), B: CardState(LEASED, 'i3', 1_000.0)}
    rows = by_uuid(accrue([box(cards=cards)], {'i3': record}, Cursors(settled_at=1_000.0), 1_072.0))
    assert rows[A].leased_s == rows[B].leased_s == 60

    record.pay_through = 1_120.0
    cards[A] = CardState(CHECKING, '', 1_100.0)  # one card of the instance is no longer ours (its id is cleared)
    lost = by_uuid(accrue([box(cards=cards)], {'i3': record}, Cursors(1_072.0, {'i3': 1_060.0}), 1_132.0))
    assert lost[A].leased_s == 0 and lost[B].leased_s == 0


def test_rows_go_to_the_day_file_with_a_rollup_and_the_cursor_survives_a_restart(tmp_path):
    record = leased_record()
    record.pay_through = 1_060.0
    state = box(cards={A: CardState(IDLE, '', 1_000.0), B: CardState(LEASED, 'i1', 1_000.0)})
    ledger = Ledger(tmp_path / 'ledger')
    assert (
        ledger.due(1_000.0) and ledger.settle([state], {'i1': record}, 1_000.0) == []
    )  # the first tick sets the clock
    assert not ledger.due(1_011.0)
    rows = ledger.settle([state], {'i1': record}, 1_072.0)
    line = json.loads((tmp_path / 'ledger' / '1970-01-01.jsonl').read_text().splitlines()[0])
    assert list(line) == ['t0', 't1', 'hotkey', 'uuid', 'gpu', 'state', 'instance', 'idle_s', 'leased_s', 'withheld']
    assert line == {
        't0': 1000.0, 't1': 1072.0, 'hotkey': 'hk1', 'uuid': A, 'gpu': 'RTX5090', 'state': IDLE, 'instance': '',
        'idle_s': 72.0, 'leased_s': 0.0, 'withheld': False,
    }  # fmt: skip
    assert by_uuid(rows)[B].leased_s == 60

    restarted = Ledger(tmp_path / 'ledger')
    assert restarted.settle([state], {'i1': record}, 1_084.0)[1].leased_s == 0  # the span is not paid twice
    rollup = json.loads((tmp_path / 'ledger' / '1970-01-01.rollup.json').read_text())
    assert rollup['hotkeys']['hk1'][A]['idle_s'] == 84.0 and rollup['hotkeys']['hk1'][B]['leased_s'] == 60.0
    assert [r.t1 for r in restarted.rows(1_000.0, 1_084.0)] == [1072.0, 1072.0, 1084.0, 1084.0]


# ---------------------------------------------------------------- the window -----------------------------------------

RATES = {'RTX5090': GpuRate('RTX5090', 0.35, 1.00, 4)}
RICH = Quote(tao_usd=400.0, alpha_tao=0.1, at=0.0, source='test')  # the pool (12.3 alpha/h) is worth $492/h
HOUR = 3_600.0


def rows_for(cards, t1=HOUR, withheld=False):
    """cards: [(hotkey, uuid, idle_s, leased_s)] as one row each."""
    return [LedgerRow(0.0, t1, hk, u, 'RTX5090', IDLE, '', idle, leased, withheld) for hk, u, idle, leased in cards]


def test_below_target_every_card_earns_its_target_rate_and_the_rest_recycles():
    rows = rows_for([('hkA', A, 0, HOUR), ('hkB', B, HOUR, 0)])
    s = settle_window(rows, {}, RATES, RICH, 0.0, HOUR)
    assert s.pool_alpha == pytest.approx(12.3) and s.pool_usd == pytest.approx(492.0)
    assert s.hotkeys['hkA'].usd == pytest.approx(1.00) and s.hotkeys['hkB'].usd == pytest.approx(0.35)
    assert s.hotkeys['hkA'].weight == pytest.approx(1.00 / 492) and s.hotkeys['hkB'].weight == pytest.approx(0.35 / 492)
    assert s.recycle_share == pytest.approx(1 - 1.35 / 492)
    gpu = s.gpus['RTX5090']
    assert (gpu.cards, gpu.fleet_scale, gpu.idle_usd_per_hr, gpu.leased_usd_per_hr) == (2.0, 1.0, 0.35, 1.0)


def test_at_target_nothing_dilutes_and_above_target_everyone_dilutes_with_leased_over_idle_preserved():
    at = settle_window(rows_for([(f'hk{i}', f'u{i}', HOUR, 0) for i in range(4)]), {}, RATES, RICH, 0.0, HOUR)
    assert at.gpus['RTX5090'].fleet_scale == 1.0 and at.hotkeys['hk0'].usd == pytest.approx(0.35)

    cards = [(f'hk{i}', f'u{i}', 0, HOUR) for i in range(4)] + [(f'hk{i}', f'u{i}', HOUR, 0) for i in range(4, 8)]
    above = settle_window(rows_for(cards), {}, RATES, RICH, 0.0, HOUR)
    gpu = above.gpus['RTX5090']
    assert gpu.cards == 8 and gpu.fleet_scale == 0.5
    assert (gpu.idle_usd_per_hr, gpu.leased_usd_per_hr) == (pytest.approx(0.175), pytest.approx(0.5))
    assert above.hotkeys['hk0'].weight / above.hotkeys['hk7'].weight == pytest.approx(1.0 / 0.35)
    assert above.paid_usd == pytest.approx(4 * 0.5 + 4 * 0.175)


def test_a_pool_worth_less_than_the_targets_scales_everyone_and_recycles_nothing():
    poor = Quote(tao_usd=226.84, alpha_tao=0.003384, at=0.0, source='test')  # 9/15 prices: the pool is ~$9.44/h
    rates = {'RTX5090': GpuRate('RTX5090', 0.35, 1.00, 64)}
    rows = rows_for([(f'hk{i}', f'u{i}', 0, HOUR) for i in range(16)] + [('hkI', 'idle', HOUR, 0)])
    s = settle_window(rows, {}, rates, poor, 0.0, HOUR)
    assert s.pool_usd == pytest.approx(12.3 * 226.84 * 0.003384)
    assert s.afford == pytest.approx(s.pool_usd / 16.35) and s.recycle_share == pytest.approx(0.0, abs=1e-9)
    assert sum(h.weight for h in s.hotkeys.values()) == pytest.approx(1.0)
    assert s.hotkeys['hk0'].weight / s.hotkeys['hkI'].weight == pytest.approx(1.0 / 0.35)


def test_the_withheld_window_is_the_failure_day_and_the_day_before_and_unrated_cards_are_unpaid():
    failed_at = 20 * DAY_S + 10 * HOUR
    assert withheld_window(failed_at) == (19 * DAY_S, 21 * DAY_S)
    benched = BoxState('hkA', withheld_from=failed_at)
    rows = [
        LedgerRow(0.0, 18 * DAY_S + 23 * HOUR, 'hkA', A, 'RTX5090', LEASED, 'i', 0.0, 100.0, False),  # paid
        LedgerRow(0.0, 19 * DAY_S + 1.0, 'hkA', A, 'RTX5090', LEASED, 'i', 50.0, 100.0, False),  # withheld after all
        LedgerRow(0.0, 20 * DAY_S + 9 * HOUR, 'hkA', A, 'RTX5090', LEASED, 'i', 0.0, 100.0, True),  # flagged at write
        LedgerRow(0.0, 21 * DAY_S, 'hkA', A, 'RTX5090', LEASED, 'i', 0.0, 100.0, False),  # the window has ended
        LedgerRow(0.0, 21 * DAY_S, 'hkB', B, 'H200', LEASED, 'i', 0.0, 100.0, False),
    ]
    s = settle_window(rows, {'hkA': benched}, RATES, RICH, 18 * DAY_S, 22 * DAY_S)
    pay = s.hotkeys['hkA']
    assert (pay.leased_s, pay.withheld_s, pay.idle_s) == (200.0, 200.0, 50.0)  # idle is never withheld
    assert s.unrated_s == 100.0 and s.hotkeys['hkB'].weight == 0.0


# ---------------------------------------------------------------- the oracle -----------------------------------------


class Scripted:
    def __init__(self, tao, alpha):
        self.taos, self.alphas, self.reads = list(tao), list(alpha), 0

    def _next(self, values):
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return value

    def tao_usd(self):
        self.reads += 1
        return self._next(self.taos)

    def alpha_tao(self):
        return self._next(self.alphas)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_the_oracle_holds_the_last_good_price_refuses_wild_reads_and_never_answers_zero():
    clock = Clock()
    boom = OracleError('down')
    inner = Scripted([boom, 300.0, boom, 0.0, math.nan, 3_000.0, 3_100.0, 290.0], [0.003])
    oracle = FailSafeOracle(
        inner, StaticOracle(400.0, 0.002), refresh_s=0.0, max_move=2.0, confirm_reads=3, clock=clock
    )

    q = oracle.quote()  # nothing good yet: the static fallback, marked held
    assert (q.tao_usd, q.alpha_tao, q.held) == (400.0, 0.003, True) and 'static fallback' in q.notes[0]
    assert (oracle.quote().tao_usd, oracle.quote().held) == (300.0, True)  # 300 good; then a failed read holds it
    assert oracle.quote().tao_usd == 300.0  # 0 is refused
    assert oracle.quote().tao_usd == 300.0  # NaN is refused
    q = oracle.quote()  # a 10x move is refused...
    assert q.tao_usd == 300.0 and q.held and 'refused a move' in q.notes[0]
    assert oracle.quote().tao_usd == 300.0  # ...and again...
    assert oracle.quote().tao_usd == 290.0  # a read back in range is taken, and clears the refused ones


def test_a_real_move_is_taken_after_consistent_reads_and_reads_are_cached_for_the_refresh():
    clock = Clock()
    inner = Scripted([300.0, 3_000.0, 3_050.0, 2_990.0], [0.003])
    oracle = FailSafeOracle(inner, refresh_s=600.0, max_move=2.0, confirm_reads=3, clock=clock)
    assert oracle.quote().tao_usd == 300.0 and inner.reads == 1
    assert oracle.quote().tao_usd == 300.0 and inner.reads == 1  # cached
    for expected in (300.0, 300.0, 2_990.0):
        clock.t += 600.0
        assert oracle.quote().tao_usd == expected
    q = oracle.quote(force=True)
    assert q.tao_usd == 2_990.0 and not q.held and q.source == 'Scripted' and q.at == clock.t


class Response:
    def __init__(self, doc):
        self.doc = doc

    def raise_for_status(self):
        pass

    def json(self):
        return self.doc


def test_metagraphed_reads_the_documented_shapes_and_refuses_stale_or_unpriced():
    docs = {
        'https://mg.example/api/v1/network/tao-usd': {
            'latest': {'usd_per_tao': 226.84, 'price_basis': 'wrapped_onchain_median'},
            'stale': False,
        },
        'https://mg.example/api/v1/subnets/74/economics': {'economics': {'alpha_price_tao': 0.003384}},
    }
    oracle = MetagraphedOracle('https://mg.example/', get=lambda url, timeout: Response(docs[url]))
    assert (oracle.tao_usd(), oracle.alpha_tao()) == (226.84, 0.003384)
    docs['https://mg.example/api/v1/network/tao-usd'] = {
        'latest': {'usd_per_tao': None, 'price_basis': 'insufficient_pools'}
    }
    with pytest.raises(OracleError, match='insufficient_pools'):
        oracle.tao_usd()
    docs['https://mg.example/api/v1/network/tao-usd'] = {'latest': {'usd_per_tao': 226.0}, 'stale': True}
    with pytest.raises(OracleError, match='stale'):
        oracle.tao_usd()

    def down(url, timeout):
        raise ConnectionError('no route')

    with pytest.raises(OracleError, match='ConnectionError'):
        MetagraphedOracle('https://mg.example', get=down).alpha_tao()


def test_coingecko_plus_chain_oracle_reads_both_and_fails_closed_on_bad_bodies():
    from gittensor.controller.pay.oracle import CoinGeckoChainOracle, OracleError

    class Resp:
        def __init__(self, doc, ok=True):
            self.doc, self.ok = doc, ok

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError('503')

        def json(self):
            return self.doc

    good = CoinGeckoChainOracle(
        'ws://x', 74, get=lambda url, timeout: Resp({'bittensor': {'usd': 226.84}}), price_reader=lambda: 0.003384
    )
    assert good.tao_usd() == 226.84 and good.alpha_tao() == 0.003384
    bad_body = CoinGeckoChainOracle('ws://x', 74, get=lambda url, timeout: Resp({'nope': 1}), price_reader=lambda: 0.1)
    with pytest.raises(OracleError, match='coingecko'):
        bad_body.tao_usd()
    down = CoinGeckoChainOracle('ws://x', 74, get=lambda url, timeout: Resp({}, ok=False), price_reader=lambda: 0.1)
    with pytest.raises(OracleError, match='coingecko'):
        down.tao_usd()
