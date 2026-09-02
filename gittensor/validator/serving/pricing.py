# The MIT License (MIT)
# Copyright © 2025 Entrius

"""What the serving pool has to pay with: alpha/hour flowing to miners and what one alpha is worth in USD.

Two inputs every validator observes identically — the metagraph emission column and the subnet's on-chain
alpha/TAO price — plus a live TAO/USD rate refetched from a public feed at most every
``SERVING_TAO_USD_REFRESH_S``. Independent validators read the same feed inside the same window, so the
share stays agreed to within spot noise; with the feed down the last fetched rate carries, and the
cold-start fallback is deliberately high so an unpriceable TAO undersizes the pool instead of overpaying it.
"""

import time
from typing import TYPE_CHECKING, Optional, Tuple

import bittensor as bt
import requests

from gittensor.classes import ServingPricing
from gittensor.constants import (
    SERVING_PRICING_MAX_AGE_S,
    SERVING_TAO_USD_FALLBACK,
    SERVING_TAO_USD_REFRESH_S,
)

if TYPE_CHECKING:
    from neurons.validator import Validator

MINER_FRACTION_OF_NEURON_EMISSION = 0.5  # metagraph.E covers miners + validators (41% + 41%); miners get half
MINUTES_PER_TEMPO = 72.0  # 360 blocks x 12 s

TAO_USD_URL = 'https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd'

_last_usable: Optional[Tuple[float, ServingPricing]] = None  # (ts, pricing): carries pay across a price-read blip
_tao_usd_cache: Optional[Tuple[float, float]] = None  # (checked_ts, rate): the last fetched TAO/USD


def _fetch_tao_usd() -> Optional[float]:
    for attempt in range(3):
        try:
            r = requests.get(TAO_USD_URL, timeout=5)
            r.raise_for_status()
            rate = float(r.json()['bittensor']['usd'])
            if rate > 0:
                return rate
        except Exception as e:
            bt.logging.warning(f'Serving: TAO/USD fetch {attempt + 1}/3 failed ({e!r})')
    return None


def tao_usd_rate(now: Optional[float] = None) -> float:
    """Live TAO/USD for sizing the pool, refetched at most every ``SERVING_TAO_USD_REFRESH_S``.

    A failed refresh (3 tries) keeps the last fetched rate until the next window; before anything has been
    fetched the fallback prices TAO deliberately high, so failure undersizes the pool rather than overpaying.
    """
    global _tao_usd_cache
    ts = now if now is not None else time.time()
    if _tao_usd_cache is not None and ts - _tao_usd_cache[0] < SERVING_TAO_USD_REFRESH_S:
        return _tao_usd_cache[1]
    rate = _fetch_tao_usd()
    if rate is None:
        if _tao_usd_cache is None:
            bt.logging.warning(f'Serving: no TAO/USD fetched yet; fallback ${SERVING_TAO_USD_FALLBACK:.0f}')
            return SERVING_TAO_USD_FALLBACK
        rate = _tao_usd_cache[1]
        bt.logging.warning(f'Serving: TAO/USD refresh failed; keeping {rate:.2f} until the next window')
    _tao_usd_cache = (ts, rate)
    return rate


def serving_pricing(self: 'Validator') -> Optional[ServingPricing]:
    try:
        alpha_per_tempo = float(sum(float(e) for e in self.metagraph.E)) * MINER_FRACTION_OF_NEURON_EMISSION
        alpha_per_hour = alpha_per_tempo * 60.0 / MINUTES_PER_TEMPO
        info = self.subtensor.subnet(int(self.metagraph.netuid))
        alpha_tao = float(getattr(info, 'price', 0.0) or 0.0)
        tao_usd = tao_usd_rate()
    except Exception as e:
        bt.logging.warning(f'Serving: pricing unavailable ({e!r})')
        return _recent_usable()
    pricing = ServingPricing(alpha_per_hour_to_miners=alpha_per_hour, alpha_usd=alpha_tao * tao_usd)
    if not pricing.usable:
        bt.logging.warning(
            f'Serving: pricing incomplete (alpha/h {alpha_per_hour:.1f}, alpha/TAO {alpha_tao:.6f}, '
            f'TAO/USD {tao_usd:.2f})'
        )
        return _recent_usable()
    bt.logging.info(
        f'Serving: pricing {alpha_per_hour:.1f} alpha/h to miners, ${pricing.alpha_usd:.3f}/alpha '
        f'(alpha/TAO {alpha_tao:.6f} x TAO/USD {tao_usd:.2f})'
    )
    global _last_usable
    _last_usable = (time.time(), pricing)
    return pricing


def _recent_usable(now: Optional[float] = None) -> Optional[ServingPricing]:
    """The last usable pricing while it is fresh enough to price this round; otherwise nothing."""
    if _last_usable is None:
        return None
    ts, pricing = _last_usable
    age = (now if now is not None else time.time()) - ts
    if age > SERVING_PRICING_MAX_AGE_S:
        bt.logging.warning(f'Serving: last usable pricing is {age / 60:.0f} min old; not pricing this round')
        return None
    bt.logging.info(f'Serving: reusing pricing from {age / 60:.0f} min ago')
    return pricing
