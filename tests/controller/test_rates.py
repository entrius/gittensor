# The MIT License (MIT)
# Copyright © 2025 Entrius

"""fleet_pay.json: the shipped table and the leased > idle invariant."""

import json

import pytest

from gittensor.controller.pay import load_rates
from gittensor.controller.pay.rates import RatesError


def test_shipped_table():
    rates = load_rates()
    r = rates['RTX5090']
    assert (r.idle_usd_per_hr, r.leased_usd_per_hr, r.target_fleet) == (0.35, 1.0, 64)
    assert r.leased_to_idle == pytest.approx(1.0 / 0.35)


def test_invariants(tmp_path):
    def table(**row):
        p = tmp_path / 'rates.json'
        p.write_text(json.dumps({'_doc': 'x', 'RTX5090': row}))
        return p

    with pytest.raises(RatesError, match='out-earn'):
        load_rates(table(idle_usd_per_hr=1.0, leased_usd_per_hr=0.5, target_fleet=8))
    with pytest.raises(RatesError, match='positive'):
        load_rates(table(idle_usd_per_hr=0.1, leased_usd_per_hr=0.5, target_fleet=0))
    with pytest.raises(RatesError, match='KeyError'):
        load_rates(table(idle_usd_per_hr=0.1, target_fleet=8))
    with pytest.raises(RatesError, match='no GPU types'):
        p = tmp_path / 'empty.json'
        p.write_text('{"_doc": "x"}')
        load_rates(p)
    with pytest.raises(RatesError):
        load_rates(tmp_path / 'missing.json')
    assert load_rates(table(idle_usd_per_hr=0, leased_usd_per_hr=1, target_fleet=1))['RTX5090'].leased_to_idle == float(
        'inf'
    )
