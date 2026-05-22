# The MIT License (MIT)
# Copyright © 2025 Entrius

from pathlib import Path
from types import SimpleNamespace

import numpy as np

from neurons.validator import Validator


class _FakeConfig:
    def __init__(self, full_path):
        self.neuron = SimpleNamespace(
            full_path=str(full_path),
            axon_off=True,
            device='cpu',
            epoch_length=1,
            disable_set_weights=False,
            name='validator',
            moving_average_alpha=0.7,
            num_concurrent_forwards=1,
        )
        self.netuid = 1
        self.logging = SimpleNamespace()
        self.wallet = SimpleNamespace(name='wallet', hotkey='hotkey')
        self.subtensor = SimpleNamespace(chain_endpoint='mock')

    def merge(self, other):
        self.neuron = other.neuron
        self.netuid = other.netuid
        self.logging = other.logging
        self.wallet = other.wallet
        self.subtensor = other.subtensor


class _FakeMetagraph:
    def __init__(self):
        self.hotkeys = ['hk0', 'hk1', 'hk2']
        self.n = 3
        self.last_update = np.zeros(3, dtype=np.int64)
        self.uids = np.arange(3)
        self.axons = []


class _FakeWallet:
    def __init__(self, config=None):
        self.hotkey = SimpleNamespace(ss58_address='hk0')


class _FakeSubtensor:
    chain_endpoint = 'mock'

    def __init__(self, config=None):
        self.config = config

    def metagraph(self, netuid):
        return _FakeMetagraph()


class _StartupValidator(Validator):
    full_path: Path
    set_weights_calls = 0

    @classmethod
    def config(cls):
        return _FakeConfig(cls.full_path)

    @classmethod
    def check_config(cls, config):
        pass

    @property
    def block(self):
        return 10

    def check_registered(self, max_retries: int = 3):
        pass

    def should_sync_metagraph(self):
        return False

    def set_weights(self):
        type(self).set_weights_calls += 1

    async def forward(self, synapse=None):
        return None


def _build_validator(tmp_path, monkeypatch):
    _StartupValidator.full_path = tmp_path
    _StartupValidator.set_weights_calls = 0
    monkeypatch.setattr('bittensor.Wallet', _FakeWallet)
    monkeypatch.setattr('bittensor.Subtensor', _FakeSubtensor)
    monkeypatch.setattr('bittensor.Dendrite', lambda wallet=None: 'fake-dendrite')
    monkeypatch.setattr('bittensor.logging.set_config', lambda config=None: None)
    monkeypatch.setattr('neurons.validator.pat_storage.ensure_pats_file', lambda: None)
    monkeypatch.setattr('neurons.validator.wandb.init', lambda **kwargs: None)
    return _StartupValidator(config=_FakeConfig(tmp_path))


def _write_state(state_path, scores, hotkeys, step=42):
    np.savez(
        state_path,
        step=step,
        scores=np.array(scores, dtype=np.float32),
        hotkeys=np.array(hotkeys),
    )


def test_startup_loads_existing_state_before_checkpoint(tmp_path, monkeypatch):
    state_path = tmp_path / 'state.npz'
    saved_scores = np.array([0.2, 0.3, 0.5], dtype=np.float32)
    saved_hotkeys = np.array(['hk0', 'hk1', 'hk2'])
    _write_state(state_path, saved_scores, saved_hotkeys)

    validator = _build_validator(tmp_path, monkeypatch)

    try:
        assert validator.step == 42
        np.testing.assert_array_equal(validator.scores, saved_scores)
        assert _StartupValidator.set_weights_calls == 0

        with np.load(state_path) as state:
            assert int(state['step']) == 42
            np.testing.assert_array_equal(state['scores'], saved_scores)
            np.testing.assert_array_equal(state['hotkeys'], saved_hotkeys)
    finally:
        validator.loop.close()


def test_startup_without_state_saves_fresh_checkpoint(tmp_path, monkeypatch):
    state_path = tmp_path / 'state.npz'

    validator = _build_validator(tmp_path, monkeypatch)

    try:
        assert validator.step == 0
        np.testing.assert_array_equal(validator.scores, np.zeros(3, dtype=np.float32))
        assert state_path.exists()

        with np.load(state_path) as state:
            assert int(state['step']) == 0
            np.testing.assert_array_equal(state['scores'], np.zeros(3, dtype=np.float32))
            np.testing.assert_array_equal(state['hotkeys'], np.array(['hk0', 'hk1', 'hk2']))
    finally:
        validator.loop.close()


def test_startup_with_corrupt_state_saves_fresh_checkpoint(tmp_path, monkeypatch):
    state_path = tmp_path / 'state.npz'
    state_path.write_bytes(b'not a numpy checkpoint')

    validator = _build_validator(tmp_path, monkeypatch)

    try:
        assert validator.step == 0
        np.testing.assert_array_equal(validator.scores, np.zeros(3, dtype=np.float32))

        with np.load(state_path) as state:
            assert int(state['step']) == 0
            np.testing.assert_array_equal(state['scores'], np.zeros(3, dtype=np.float32))
            np.testing.assert_array_equal(state['hotkeys'], np.array(['hk0', 'hk1', 'hk2']))
    finally:
        validator.loop.close()
