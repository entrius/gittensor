# The MIT License (MIT)
# Copyright © 2025 Entrius

"""What the serving pool has to pay with: alpha/hour flowing to miners and what one alpha is worth in USD.

Both inputs are ones every validator observes identically — the metagraph emission column and the subnet's
on-chain alpha/TAO price — plus the TAO/USD rate published in the loadout, so validators agree on the share.
"""

import time
from typing import TYPE_CHECKING, Optional, Tuple

import bittensor as bt

from gittensor.classes import ServingPricing
from gittensor.constants import SERVING_PRICING_MAX_AGE_S
from gittensor.serving.loadout import load_serving_loadout

if TYPE_CHECKING:
    from neurons.validator import Validator

MINER_FRACTION_OF_NEURON_EMISSION = 0.5  # metagraph.E covers miners + validators (41% + 41%); miners get half
MINUTES_PER_TEMPO = 72.0  # 360 blocks x 12 s

_last_usable: Optional[Tuple[float, ServingPricing]] = None  # (ts, pricing): carries pay across a price-read blip


def serving_pricing(self: 'Validator') -> Optional[ServingPricing]:
    try:
        alpha_per_tempo = float(sum(float(e) for e in self.metagraph.E)) * MINER_FRACTION_OF_NEURON_EMISSION
        alpha_per_hour = alpha_per_tempo * 60.0 / MINUTES_PER_TEMPO
        info = self.subtensor.subnet(int(self.metagraph.netuid))
        alpha_tao = float(getattr(info, 'price', 0.0) or 0.0)
        tao_usd = load_serving_loadout().tao_usd or 0.0
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
