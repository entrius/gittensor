# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Pay (``24`` §3 WS-F). Only the rate table is here for now; the per-card, per-block ledger that reads it comes
with the lease lifecycle."""

from gittensor.controller.pay.rates import DEFAULT_RATES_PATH, GpuRate, load_rates

__all__ = ['DEFAULT_RATES_PATH', 'GpuRate', 'load_rates']
