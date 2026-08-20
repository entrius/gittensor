# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared fixtures for CLI tests."""

import sys
import types

import pytest
from click.testing import CliRunner

_ORIGINAL_BITTENSOR = sys.modules.get('bittensor')
_STUBBED_BITTENSOR = False


def _ensure_fake_bittensor():
    """Provide a minimal bittensor stub when dependency is unavailable."""
    global _STUBBED_BITTENSOR

    if 'bittensor' in sys.modules:
        return
    try:
        __import__('bittensor')
        return
    except ImportError:
        pass

    class _FakeLogger:
        @staticmethod
        def debug(*args, **kwargs):
            return None

        @staticmethod
        def info(*args, **kwargs):
            return None

        @staticmethod
        def warning(*args, **kwargs):
            return None

        @staticmethod
        def error(*args, **kwargs):
            return None

    class _FakeWallet:
        def __init__(self, name=None, hotkey=None):
            self.name = name
            self.hotkey = types.SimpleNamespace(ss58_address='5FakeHotkeyFromStub')

    class _FakeSubtensor:
        def __init__(self, network=None):
            self.network = network

        def is_hotkey_registered(self, *args, **kwargs):
            return True

    fake_bt = types.SimpleNamespace(
        logging=_FakeLogger(),
        Wallet=_FakeWallet,
        Subtensor=_FakeSubtensor,
    )
    sys.modules['bittensor'] = fake_bt  # type: ignore[assignment]
    _STUBBED_BITTENSOR = True


_ensure_fake_bittensor()


def pytest_unconfigure(config):
    """Restore original bittensor module state after CLI test session."""
    del config
    if not _STUBBED_BITTENSOR:
        return
    if _ORIGINAL_BITTENSOR is None:
        sys.modules.pop('bittensor', None)
    else:
        sys.modules['bittensor'] = _ORIGINAL_BITTENSOR


def _get_cli_root():
    """Return the root Click group."""
    from gittensor.cli.main import cli

    return cli


@pytest.fixture
def cli_root():
    return _get_cli_root()


@pytest.fixture
def runner():
    return CliRunner()
