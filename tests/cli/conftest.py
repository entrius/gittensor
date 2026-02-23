# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared fixtures for CLI tests."""

import sys
import types

import click
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
    sys.modules['bittensor'] = fake_bt
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
    """Return root Click group with issue commands registered."""
    try:
        from gittensor.cli.main import cli

        return cli
    except ImportError:
        from gittensor.cli.issue_commands import register_commands

        root = click.Group()
        register_commands(root)
        return root


@pytest.fixture
def cli_root():
    return _get_cli_root()


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def sample_issue():
    return {
        'id': 42,
        'repository_full_name': 'entrius/gittensor',
        'issue_number': 223,
        'status': 'Active',
    }


@pytest.fixture
def sample_prs():
    return [
        {
            'number': 101,
            'title': 'Fix issue #223',
            'author_login': 'alice',
            'state': 'OPEN',
            'created_at': '2026-02-01T10:00:00Z',
            'merged_at': None,
            'url': 'https://github.com/entrius/gittensor/pull/101',
            'review_count': 1,
            'closing_numbers': [223],
        },
        {
            'number': 103,
            'title': 'Alternative approach',
            'author_login': 'bob',
            'state': 'OPEN',
            'created_at': '2026-02-02T11:00:00Z',
            'merged_at': None,
            'url': 'https://github.com/entrius/gittensor/pull/103',
            'review_count': 0,
            'closing_numbers': [],
        },
    ]


@pytest.fixture
def sample_prs_missing_closing():
    return [
        {
            'number': 200,
            'title': 'No closing refs',
            'author_login': 'charlie',
            'state': 'OPEN',
            'created_at': '2026-02-03T12:00:00Z',
            'merged_at': None,
            'url': 'https://github.com/entrius/gittensor/pull/200',
            'review_count': 0,
        }
    ]
