import asyncio
from types import SimpleNamespace
from typing import cast

from neurons.base.validator import BaseValidatorNeuron


class _StubValidator:
    def __init__(self, configured_forwards: int):
        self.calls = 0
        self.config = SimpleNamespace(neuron=SimpleNamespace(num_concurrent_forwards=configured_forwards))

    async def forward(self):
        self.calls += 1


def test_concurrent_forward_clamps_validator_rounds_to_single_flight():
    validator = _StubValidator(configured_forwards=3)

    asyncio.run(BaseValidatorNeuron.concurrent_forward(cast(BaseValidatorNeuron, validator)))

    assert validator.calls == 1
    assert validator.config.neuron.num_concurrent_forwards == 1


def test_concurrent_forward_preserves_default_single_round():
    validator = _StubValidator(configured_forwards=1)

    asyncio.run(BaseValidatorNeuron.concurrent_forward(cast(BaseValidatorNeuron, validator)))

    assert validator.calls == 1
    assert validator.config.neuron.num_concurrent_forwards == 1
