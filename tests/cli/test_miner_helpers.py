# Entrius 2025

"""Tests for miner CLI helpers (hotkey resolution)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_resolve_hotkey_returns_name_when_file_exists(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    hk = tmp_path / '.bittensor' / 'wallets' / 'alice' / 'hotkeys' / 'myhotkey'
    hk.parent.mkdir(parents=True)
    hk.write_text('x')

    from gittensor.cli.miner_commands.helpers import _resolve_wallet_hotkey_name

    assert _resolve_wallet_hotkey_name('alice', 'myhotkey') == 'myhotkey'


def test_resolve_hotkey_maps_ss58_to_file_name(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    hdir = tmp_path / '.bittensor' / 'wallets' / 'alice' / 'hotkeys'
    hdir.mkdir(parents=True)
    (hdir / 'minerhk').write_text('placeholder')

    def fake_wallet(name: str, hotkey: str):
        w = MagicMock()
        if hotkey == 'minerhk':
            w.hotkey.ss58_address = '5FtEXFJHQkV6wgjT1By59zUSZHVE4Ah3FbDynEYnmUBmRZxK'
        else:
            w.hotkey.ss58_address = '5Other'
        return w

    from gittensor.cli.miner_commands.helpers import _resolve_wallet_hotkey_name

    with patch('bittensor.Wallet', side_effect=fake_wallet):
        out = _resolve_wallet_hotkey_name('alice', '5FtEXFJHQkV6wgjT1By59zUSZHVE4Ah3FbDynEYnmUBmRZxK')
        assert out == 'minerhk'


def test_resolve_hotkey_unknown_lists_available(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    hdir = tmp_path / '.bittensor' / 'wallets' / 'alice' / 'hotkeys'
    hdir.mkdir(parents=True)
    (hdir / 'hk1').write_text('a')

    from gittensor.cli.miner_commands.helpers import _resolve_wallet_hotkey_name

    with pytest.raises(ValueError) as exc:
        _resolve_wallet_hotkey_name('alice', 'not-a-key')
    assert 'hk1' in str(exc.value)
    assert 'Available names' in str(exc.value)
