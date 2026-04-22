# Entrius 2025

"""Tests for miner CLI wallet directory guard."""

from pathlib import Path

import pytest

from gittensor.cli.miner_commands.helpers import _require_wallet_directory


def test_require_wallet_directory_ok(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    cold = tmp_path / '.bittensor' / 'wallets' / 'alice'
    cold.mkdir(parents=True)
    _require_wallet_directory('alice')


def test_require_wallet_directory_missing_lists_known(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    root = tmp_path / '.bittensor' / 'wallets'
    root.mkdir(parents=True)
    (root / 'bob').mkdir()
    with pytest.raises(ValueError) as exc:
        _require_wallet_directory('missing')
    assert 'missing' in str(exc.value)
    assert 'bob' in str(exc.value)
