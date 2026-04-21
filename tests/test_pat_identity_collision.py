# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for GitHub identity collision handling in PAT broadcasts."""

import asyncio
import importlib
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace


def _install_pat_handler_stubs(monkeypatch):
    fake_bt = SimpleNamespace(
        Synapse=object,
        logging=SimpleNamespace(
            warning=lambda *_args, **_kwargs: None,
            success=lambda *_args, **_kwargs: None,
            info=lambda *_args, **_kwargs: None,
        ),
    )
    monkeypatch.setitem(sys.modules, 'bittensor', fake_bt)

    fake_constants = ModuleType('gittensor.constants')
    fake_constants.BASE_GITHUB_API_URL = 'https://api.github.com'
    fake_constants.GITHUB_HTTP_TIMEOUT_SECONDS = 15
    fake_constants.GRAPHQL_VIEWER_QUERY = '{ viewer { login } }'
    monkeypatch.setitem(sys.modules, 'gittensor.constants', fake_constants)

    fake_synapses = ModuleType('gittensor.synapses')

    class FakePatBroadcastSynapse:
        def __init__(self, github_access_token):
            self.github_access_token = github_access_token
            self.accepted = False
            self.rejection_reason = None
            self.dendrite = None

    fake_synapses.PatBroadcastSynapse = FakePatBroadcastSynapse
    fake_synapses.PatCheckSynapse = object
    monkeypatch.setitem(sys.modules, 'gittensor.synapses', fake_synapses)

    fake_validation = ModuleType('gittensor.validator.utils.github_validation')
    fake_validation.validate_github_credentials = lambda *_args, **_kwargs: ('github_42', None)
    monkeypatch.setitem(sys.modules, 'gittensor.validator.utils.github_validation', fake_validation)

    monkeypatch.delitem(sys.modules, 'gittensor.validator.pat_handler', raising=False)


def _make_validator(hotkeys):
    return SimpleNamespace(metagraph=SimpleNamespace(hotkeys=hotkeys, S=[0.0] * len(hotkeys)))


def _make_synapse(module, hotkey, pat='ghp_test'):
    synapse = module.PatBroadcastSynapse(github_access_token=pat)
    synapse.dendrite = SimpleNamespace(hotkey=hotkey)
    return synapse


def test_active_duplicate_github_id_is_rejected(monkeypatch):
    _install_pat_handler_stubs(monkeypatch)
    from gittensor.validator import pat_storage

    with TemporaryDirectory() as tmp_dir:
        monkeypatch.setattr(pat_storage, 'PATS_FILE', Path(tmp_dir) / 'miner_pats.json')
        pat_storage.save_pat(0, 'hotkey_0', 'ghp_old', 'github_42')

        pat_handler = importlib.import_module('gittensor.validator.pat_handler')
        monkeypatch.setattr(pat_handler, '_test_pat_against_repo', lambda *_args, **_kwargs: None)

        validator = _make_validator(['hotkey_0', 'hotkey_1'])
        synapse = _make_synapse(pat_handler, 'hotkey_1')

        result = asyncio.run(pat_handler.handle_pat_broadcast(validator, synapse))

        assert result.accepted is False
        assert 'already registered' in (result.rejection_reason or '').lower()
        assert pat_storage.get_pat_by_uid(1) is None


def test_stale_duplicate_github_id_is_reclaimed(monkeypatch):
    _install_pat_handler_stubs(monkeypatch)
    from gittensor.validator import pat_storage

    with TemporaryDirectory() as tmp_dir:
        monkeypatch.setattr(pat_storage, 'PATS_FILE', Path(tmp_dir) / 'miner_pats.json')
        pat_storage.save_pat(9, 'old_hotkey', 'ghp_old', 'github_42')

        pat_handler = importlib.import_module('gittensor.validator.pat_handler')
        monkeypatch.setattr(pat_handler, '_test_pat_against_repo', lambda *_args, **_kwargs: None)

        validator = _make_validator(['hotkey_1'])
        synapse = _make_synapse(pat_handler, 'hotkey_1', pat='ghp_new')

        result = asyncio.run(pat_handler.handle_pat_broadcast(validator, synapse))

        assert result.accepted is True
        entry = pat_storage.get_pat_by_uid(0)
        assert entry is not None
        assert entry['github_id'] == 'github_42'
        assert entry['hotkey'] == 'hotkey_1'
        assert len(pat_storage.load_all_pats()) == 1
