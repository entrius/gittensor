# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Pay (``24`` §3 WS-F): the rate table (``rates.py``, ``fleet_pay.json``), the per-card per-block ledger and the
window's single weighted pool (``ledger.py``), the fail-safe price oracle (``oracle.py``) and the signed scorecard the
validator reads (``scorecard.py``)."""

from gittensor.controller.pay.rates import DEFAULT_RATES_PATH, GpuRate, load_rates

__all__ = ['DEFAULT_RATES_PATH', 'GpuRate', 'load_rates']
