# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Tests for local preference resolution and backend publishing."""

import json

from gittensor.validator.utils.load_weights import RepositoryConfig
from gittensor.validator.weight_consensus.publisher import maybe_publish_prefs, resolve_local_prefs

MASTER = {
    'a/big': RepositoryConfig(emission_share=0.6),
    'b/small': RepositoryConfig(emission_share=0.4),
    'c/zero': RepositoryConfig(emission_share=0.0),
}


class FakeBackend:
    def __init__(self, own_basket=None, publish_ok=True):
        self.own_basket = own_basket
        self.publish_ok = publish_ok
        self.published = []

    def fetch_own_basket(self):
        return self.own_basket

    def publish_basket(self, prefs):
        if isinstance(self.publish_ok, Exception):
            raise self.publish_ok
        if self.publish_ok:
            self.published.append(prefs)
        return bool(self.publish_ok)


class TestResolveLocalPrefs:
    def test_default_vote_from_baked_master_shares(self, tmp_path):
        prefs = resolve_local_prefs(tmp_path / 'missing.json', MASTER)
        assert set(prefs) == {'a/big', 'b/small'}
        assert prefs['a/big'] > prefs['b/small']

    def test_prefs_file_parsed(self, tmp_path):
        path = tmp_path / 'prefs.json'
        path.write_text(json.dumps({'version': 1, 'repos': {'X/Y': 3, 'z/w': 1}}))
        prefs = resolve_local_prefs(path, MASTER)
        assert set(prefs) == {'x/y', 'z/w'}

    def test_invalid_file_falls_back_to_default(self, tmp_path):
        path = tmp_path / 'prefs.json'
        path.write_text('{not json')
        assert set(resolve_local_prefs(path, MASTER)) == {'a/big', 'b/small'}


class TestMaybePublish:
    def test_skips_when_onchain_basket_equal(self):
        prefs = {'a/b': 60000, 'c/d': 5535}
        backend = FakeBackend(own_basket={'a/b': 60000, 'c/d': 5535})
        assert maybe_publish_prefs(backend, prefs) is True
        assert backend.published == []

    def test_publishes_when_onchain_differs(self):
        backend = FakeBackend(own_basket={'a/b': 65535})
        assert maybe_publish_prefs(backend, {'c/d': 65535}) is True
        assert backend.published == [{'c/d': 65535}]

    def test_publish_rejection_returns_false(self):
        backend = FakeBackend(publish_ok=False)
        assert maybe_publish_prefs(backend, {'a/b': 65535}) is False

    def test_publish_exception_returns_false(self):
        backend = FakeBackend(publish_ok=RuntimeError('rate limited'))
        assert maybe_publish_prefs(backend, {'a/b': 65535}) is False
