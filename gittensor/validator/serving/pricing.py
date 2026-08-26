# The MIT License (MIT)
# Copyright © 2025 Entrius

"""What the serving pool has to pay with: alpha/hour flowing to miners and what one alpha is worth in USD.

Both inputs are ones every validator observes identically — the metagraph emission column and the subnet's
on-chain alpha/TAO price — plus the TAO/USD rate published in the loadout, so validators agree on the share.
"""

from typing import TYPE_CHECKING, Optional

import bittensor as bt

from gittensor.classes import ServingPricing
from gittensor.serving.loadout import load_serving_loadout

if TYPE_CHECKING:
    from neurons.validator import Validator

MINER_FRACTION_OF_NEURON_EMISSION = 0.5  # metagraph.E covers miners + validators (41% + 41%); miners get half
MINUTES_PER_TEMPO = 72.0  # 360 blocks x 12 s


def serving_pricing(self: 'Validator') -> Optional[ServingPricing]:
    try:
        alpha_per_tempo = float(sum(float(e) for e in self.metagraph.E)) * MINER_FRACTION_OF_NEURON_EMISSION
        alpha_per_hour = alpha_per_tempo * 60.0 / MINUTES_PER_TEMPO
        info = self.subtensor.subnet(int(self.metagraph.netuid))
        alpha_tao = float(getattr(info, 'price', 0.0) or 0.0)
        tao_usd = load_serving_loadout().tao_usd or 0.0
    except Exception as e:
        bt.logging.warning(f'Serving: pricing unavailable ({e!r}); paying the cap pro-rata')
        return None
    pricing = ServingPricing(alpha_per_hour_to_miners=alpha_per_hour, alpha_usd=alpha_tao * tao_usd)
    if not pricing.usable:
        bt.logging.warning(
            f'Serving: pricing incomplete (alpha/h {alpha_per_hour:.1f}, alpha/TAO {alpha_tao:.6f}, '
            f'TAO/USD {tao_usd:.2f}); paying the cap pro-rata'
        )
        return None
    bt.logging.info(
        f'Serving: pricing {alpha_per_hour:.1f} alpha/h to miners, ${pricing.alpha_usd:.3f}/alpha '
        f'(alpha/TAO {alpha_tao:.6f} x TAO/USD {tao_usd:.2f})'
    )
    return pricing
