# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Regression tests for validator weight emission safety."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import numpy as np

from neurons.base.validator import BaseValidatorNeuron


class _DummyValidator:
    def __init__(self):
        self.scores = np.array([0.2, np.nan, 0.3], dtype=np.float32)
        self.metagraph = SimpleNamespace(uids=np.array([0, 1, 2]))
        self.config = SimpleNamespace(netuid=74)
        self.subtensor = MagicMock()
        self.subtensor.set_weights.return_value = (True, '')
        self.wallet = MagicMock()
        self.spec_version = 1


def test_set_weights_sanitizes_nan_scores_before_processing():
    validator = _DummyValidator()
    captured = {}

    def _capture_processed_weights(uids, weights, **_kwargs):
        captured['weights'] = weights.copy()
        return uids, weights

    with (
        patch('neurons.base.validator.process_weights_for_netuid', side_effect=_capture_processed_weights),
        patch('neurons.base.validator.convert_weights_and_uids_for_emit', return_value=([0, 2], [65535, 65535])),
    ):
        BaseValidatorNeuron.set_weights(cast(BaseValidatorNeuron, validator))

    assert np.array_equal(validator.scores, np.array([0.2, 0.0, 0.3], dtype=np.float32))
    assert not np.isnan(captured['weights']).any()
    assert captured['weights'][1] == 0.0
    assert captured['weights'].sum() == np.float32(1.0)
    validator.subtensor.set_weights.assert_called_once()
