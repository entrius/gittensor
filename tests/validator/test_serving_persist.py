"""Serving round persistence: the round report is turned into DB rows and never fails a round."""

import datetime as dt
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

persist = pytest.importorskip('gittensor.validator.serving.persist')
forward = pytest.importorskip('gittensor.validator.serving.forward')
state_module = pytest.importorskip('gittensor.serving.state')
classes = pytest.importorskip('gittensor.classes')
loadout = pytest.importorskip('gittensor.serving.loadout')

ROUND = {
    'served': 4,
    'gateway': 1,
    'baseline': 3,
    'pass': 3,
    'miss': 1,
    'strike': 0,
    'neutral': 0,
    'ready': 1,
    'probation': 1,
    'quarantined': 0,
    'windows': {
        16: {
            'passed': True,
            'n_audits': 10,
            'mean': 0.9,
            'threshold': 0.8,
            'quarantined_until': 0.0,
            'hotkey': 'hk16',
            'model_id': 'qwen',
            'served': 3,
            'credit': 1.0,
            'ttft_ms': 48.3,
            'decode_tps': 190.0,
            'capacity': 1.0,
            'score': 1.0,
            'last_miss': '',
            'status': 'ready',
        },
        27: {
            'passed': False,
            'n_audits': 2,
            'mean': 0.5,
            'threshold': 0.8,
            'quarantined_until': 1_800_000_000.0,
            'hotkey': 'hk27',
            'model_id': 'qwen',
            'served': 1,
            'credit': 0.0,
            'ttft_ms': None,
            'decode_tps': None,
            'capacity': 0.0,
            'score': 0.0,
            'last_miss': 'tokenization mismatch (3 vs 4)',
            'status': 'quarantined',
        },
    },
}


def _state():
    st = state_module.ServingState()
    st.last_round = dict(ROUND)
    st.last_round_ts = 1_700_000_000.0
    st._history = {'hk16': deque([1.0] * 6, maxlen=12), 'hk27': deque([0.5] * 2, maxlen=12)}
    return st


def test_round_rows_shape_and_pricing():
    pricing = classes.ServingPricing(alpha_per_hour_to_miners=100.0, alpha_usd=1.0)
    ts = dt.datetime(2026, 8, 27, tzinfo=dt.timezone.utc)
    summary, miners = persist.round_rows('vali', ts, ROUND, {'hk16': 0.5, 'hk27': 1 / 12}, pricing)
    assert summary[:3] == ('vali', ts, 4)
    assert summary[12] == pytest.approx(0.5 + 1 / 12)  # card equivalents = settled sum
    assert 0 < summary[13] <= 0.035  # priced share inside the cap
    assert summary[14:16] == (100.0, 1.0)
    assert summary[16:18] == (0.70, 0.035)  # economics ride along so das never hard-codes them
    assert summary[18:] == (None, None, None, None)
    assert len(miners) == 2
    ready = next(m for m in miners if m[2] == 16)
    assert ready[5] == 'ready' and ready[12] == 48.3 and ready[13] == 190.0 and ready[16] == 0.5 and ready[17] is None
    quarantined = next(m for m in miners if m[2] == 27)
    assert quarantined[5] == 'quarantined'
    assert quarantined[9] == dt.datetime.fromtimestamp(1_800_000_000.0, dt.timezone.utc)
    assert quarantined[17].startswith('tokenization mismatch')


def test_round_rows_carry_the_enforced_release():
    release = loadout.ServingRelease.from_dict(
        {
            'model_id': 'qwen',
            'backend': 'openai-compat',
            'runtime_pin': 'org/sparkinfer@abc',
            'model_sha256': 'ff',
            'model_file': 'org/model.gguf',
        }
    )
    summary, _ = persist.round_rows('vali', dt.datetime.now(dt.timezone.utc), ROUND, {}, None, release)
    assert summary[18:] == ('qwen', 'org/sparkinfer@abc', 'ff', 'org/model.gguf')


def test_default_loadout_keeps_model_file():
    assert loadout.load_serving_loadout().primary.model_file.endswith('.gguf')


def test_round_rows_without_pricing_pays_cap_pro_rata():
    summary, _ = persist.round_rows('vali', dt.datetime.now(dt.timezone.utc), ROUND, {'hk16': 1.0}, None)
    assert summary[13] == 0.035 and summary[14] is None and summary[15] is None


def test_store_round_writes_and_prunes():
    conn = MagicMock()
    conn.closed = False
    cur = conn.cursor.return_value.__enter__.return_value
    with patch.object(persist, 'create_database_connection', return_value=conn):
        storage = persist.ServingRoundStorage()
        assert storage.store_round('vali', _state(), None) is True
    assert cur.execute.call_count == 3  # summary insert + 2 prunes
    assert cur.executemany.call_count == 1 and len(cur.executemany.call_args.args[1]) == 2
    assert cur.execute.call_args_list[1].args[1] == ('vali', 7)
    conn.commit.assert_called_once()


def test_store_round_failure_is_swallowed_and_reconnects():
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError('db down')
    with patch.object(persist, 'create_database_connection', return_value=conn) as factory:
        storage = persist.ServingRoundStorage()
        assert storage.store_round('vali', _state(), None) is False
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()
        assert storage._conn is None
        storage.store_round('vali', _state(), None)
        assert factory.call_count == 2


def test_store_round_skips_before_first_round():
    with patch.object(persist, 'create_database_connection') as factory:
        assert persist.ServingRoundStorage().store_round('vali', state_module.ServingState(), None) is False
        factory.assert_not_called()


def test_miner_status():
    assert forward.miner_status({'score': 0.9}) == 'ready'
    assert forward.miner_status({'score': 0.0, 'quarantined_until': 5.0}) == 'quarantined'
    assert forward.miner_status({'score': 0.0, 'quarantined_until': 0.0}) == 'probation'


def test_settled_scores():
    assert _state().settled_scores() == {'hk16': pytest.approx(0.5), 'hk27': pytest.approx(1 / 12)}
