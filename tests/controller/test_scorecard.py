# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The signed scorecard: its sha256 and the checks a validator makes (tampered, stale, from the future, weights that are
not a split of one pool), UUIDs only ever hashed with a per-scorecard salt, the TTL, and the daemon writing one from its
own ledger with `gitt controller scorecard` and `status` reading it."""

import hashlib
import json
from typing import Any, cast

import pytest

import gittensor.cli.main  # noqa: F401  (the CLI package must load before gittensor.controller.cli: circular import)
from gittensor.controller import cli as ctl
from gittensor.controller.checks.state import IDLE, LEASED, BoxState, CardState, StateStore
from gittensor.controller.daemon import Controller, Intervals
from gittensor.controller.pay.ledger import LedgerRow, settle_window
from gittensor.controller.pay.oracle import FailSafeOracle, Quote, StaticOracle
from gittensor.controller.pay.rates import load_rates
from gittensor.controller.pay.scorecard import (
    SCHEMA,
    ScorecardError,
    build_scorecard,
    canonical_bytes,
    read_scorecard,
    uuid_hash,
    write_scorecard,
)
from gittensor.controller.registry import Registry
from gittensor.controller.standing import CLEAN_LEASE
from tests.controller.test_cli import invoke

UUID_A = 'GPU-4f2a6b8c-1d3e-4a5b-9c7d-0e1f2a3b4c5d'
UUID_B = 'GPU-9b8c7d6e-5f40-4132-a2b3-c4d5e6f70819'
HK_A = '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY'
HK_B = '5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty'
ISSUED = 1_789_000_000.0  # 2026-09-10
HOUR = 3_600.0


def example_scorecard(salt: str = 'c0ffee' * 5 + 'ab') -> dict:
    """The worked example: one box leased for 40 min and idle for 20 on card A, idle all hour on card B; a second box
    benched with its pay withheld. 9/15 prices."""
    rates = load_rates()
    boxes = {
        HK_A: BoxState(
            HK_A, status=IDLE, pinned_uuids=[UUID_A, UUID_B], card_name='NVIDIA GeForce RTX 5090',
            last_check_at=ISSUED - 300, cards={UUID_A: CardState(LEASED, 'i-1', ISSUED - 2_400),
                                               UUID_B: CardState(IDLE, '', ISSUED - 7_200)},
            standing_events=[{'at': ISSUED - 30 * HOUR, 'kind': CLEAN_LEASE, 'leased_s': 7 * HOUR}],
        ),
        HK_B: BoxState(HK_B, status='BENCHED', withheld_from=ISSUED - 600, last_check_at=ISSUED - 1_500),
    }  # fmt: skip
    rows = [
        LedgerRow(ISSUED - HOUR, ISSUED, HK_A, UUID_A, 'RTX5090', LEASED, 'i-1', 1_200.0, 2_400.0, False),
        LedgerRow(ISSUED - HOUR, ISSUED, HK_A, UUID_B, 'RTX5090', IDLE, '', 3_600.0, 0.0, False),
        LedgerRow(ISSUED - HOUR, ISSUED - 600, HK_B, UUID_A, 'RTX5090', LEASED, 'i-9', 0.0, 3_000.0, False),
    ]
    quote = Quote(tao_usd=226.84, alpha_tao=0.003384, at=ISSUED - 120, source='MetagraphedOracle')
    settlement = settle_window(rows, boxes, rates, quote, ISSUED - HOUR, ISSUED)
    return build_scorecard(settlement, boxes, rates, ISSUED, salt=salt)


def test_the_example_scorecard_pays_the_window_hashes_every_uuid_and_withholds_the_benched_box(tmp_path):
    doc = example_scorecard()
    assert doc['schema'] == SCHEMA and doc['valid_until'] == ISSUED + 2 * 1_200
    assert [h['hotkey'] for h in doc['hotkeys']] == sorted([HK_A, HK_B])
    a, b = (next(h for h in doc['hotkeys'] if h['hotkey'] == hk) for hk in (HK_A, HK_B))
    assert a['standing'] == 'standard' and a['leased_s'] == 2_400 and a['idle_s'] == 4_800
    assert a['usd'] == pytest.approx(2_400 / HOUR * 1.00 + 4_800 / HOUR * 0.35)  # the pool affords the targets
    assert b['withheld'] and b['withheld_s'] == 3_000 and b['weight'] == 0 and b['cards'] == []
    assert a['weight'] + doc['recycle_share'] == pytest.approx(1.0)
    assert [c['uuid_hash'] for c in a['cards']] == sorted(
        [uuid_hash(doc['salt'], UUID_A), uuid_hash(doc['salt'], UUID_B)]
    )

    path, sha = write_scorecard(tmp_path / 'scorecard', doc)
    body = path.read_bytes()
    assert UUID_A not in body.decode() and UUID_B not in body.decode()  # never raw in anything published
    assert sha == hashlib.sha256(body).hexdigest() and (
        tmp_path / 'scorecard' / 'latest.sha256'
    ).read_text().startswith(sha)
    assert list((tmp_path / 'scorecard' / '2026-09-10').glob(f'*-{sha[:12]}.json'))  # the dated evidence copy
    assert read_scorecard(path, ISSUED + 1)[1] == sha
    again = next(h for h in example_scorecard(salt='another')['hotkeys'] if h['hotkey'] == HK_A)
    assert {c['uuid_hash'] for c in again['cards']}.isdisjoint(c['uuid_hash'] for c in a['cards'])  # not followable


def test_a_validator_refuses_a_tampered_stale_future_or_unbalanced_scorecard(tmp_path):
    path, _ = write_scorecard(tmp_path, example_scorecard())
    with pytest.raises(ScorecardError, match='stale'):
        read_scorecard(path, ISSUED + 2 * 1_200)  # the TTL: two intervals, then refused
    with pytest.raises(ScorecardError, match='future'):
        read_scorecard(path, ISSUED - 1_000)

    tampered = path.read_bytes().replace(b'"weight":0.0', b'"weight":0.5', 1)
    path.write_bytes(tampered)
    with pytest.raises(ScorecardError, match='does not match'):
        read_scorecard(path, ISSUED + 1)

    doc = example_scorecard()
    doc['recycle_share'] = 0.9  # the weights no longer split one pool, even with a matching hash
    path, _ = write_scorecard(tmp_path, doc)
    with pytest.raises(ScorecardError, match='split of one pool'):
        read_scorecard(path, ISSUED + 1)
    (tmp_path / 'latest.sha256').unlink()
    with pytest.raises(ScorecardError):
        read_scorecard(path, ISSUED + 1)
    path.write_bytes(canonical_bytes({'schema': 'something-else'}))
    (tmp_path / 'latest.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(ScorecardError, match='not a gt-compute-scorecard'):
        read_scorecard(path, ISSUED + 1)


def test_the_daemon_writes_a_scorecard_from_its_own_ledger_and_the_cli_reads_it(tmp_path, monkeypatch):
    monkeypatch.setenv('COLUMNS', '250')
    root = tmp_path / 'state'
    root.mkdir()
    now = __import__('time').time()
    StateStore(root / 'boxes.json').put(
        BoxState(HK_A, status=IDLE, pinned_uuids=[UUID_A], card_name='NVIDIA GeForce RTX 5090', last_check_at=now - 60,
                 cards={UUID_A: CardState(IDLE, '', now - 60)}, host='10.0.0.1', port=2200)
    )  # fmt: skip
    oracle = FailSafeOracle(StaticOracle(226.84, 0.003384))
    controller = Controller(
        ctl.StateDir(root), Registry(root / 'registry', 'unused'), make_runner=cast(Any, None),
        run_round=cast(Any, None), load_proof=cast(Any, None), intervals=Intervals(), oracle=oracle,
    )  # fmt: skip
    assert controller.settle_once(now - 36) == 0  # the first tick only starts the clock
    assert controller.settle_once(now - 30) is None  # not due inside a block
    assert controller.settle_once(now - 24) == 1 and controller.settle_once(now) == 1
    doc = controller.scorecard_once(now)
    (entry,) = doc['hotkeys']
    assert entry['idle_s'] == pytest.approx(36, abs=0.01) and entry['weight'] > 0
    status = json.loads((root / 'controller.json').read_text())['pay']
    assert status['sha256'] and status['implied_usd_per_card_hour']['RTX5090']['idle'] == pytest.approx(0.35)

    shown = invoke('scorecard', '--state-dir', root, '--json')
    payload = json.loads(shown.stdout)
    assert shown.exit_code == 0 and payload['valid'] and payload['scorecard']['hotkeys'][0]['hotkey'] == HK_A
    assert invoke('scorecard', '--state-dir', root).exit_code == 0
    state = json.loads(invoke('status', '--state-dir', root, '--json').stdout)
    assert state['pay']['valid'] and state['boxes'][0]['standing'] == 'probation'
    assert state['boxes'][0]['pay']['idle_s'] == pytest.approx(36, abs=0.01)
    text = invoke('status', '--state-dir', root).output
    assert 'pay: scorecard' in text and 'probation' in text

    assert invoke('scorecard', '--state-dir', tmp_path / 'empty').exit_code == 2
