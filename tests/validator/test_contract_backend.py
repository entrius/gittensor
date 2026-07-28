# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Tests for the contract-backed consensus transport: block-hash pinning,
id -> name resolution, and the set_basket publish path."""

from types import SimpleNamespace

import pytest

from gittensor.validator.weight_consensus.contract_backend import ContractBackend

SNAPSHOT_HASH = '0xsnap'
HEAD_HASH = '0xhead'


def _repo(github_id, full_name):
    return SimpleNamespace(github_id=github_id, full_name=full_name)


class FakeClient:
    def __init__(self, repos, baskets=None, own_basket=None):
        self.repos = repos  # [(github_id, full_name)]
        self.baskets = baskets or {}  # hotkey -> [(github_id, weight)]
        self.own_basket = own_basket
        self.read_ats = []
        self.set_basket_calls = []

    def get_all_repos(self, at=None):
        self.read_ats.append(at)
        return [_repo(gid, name) for gid, name in self.repos]

    def get_all_baskets(self, at=None):
        self.read_ats.append(at)
        return dict(self.baskets)

    def get_basket(self, hotkey, at=None):
        self.read_ats.append(at)
        return self.own_basket

    def set_basket(self, entries, wallet):
        self.set_basket_calls.append((entries, wallet))
        return True


class FakeSubstrate:
    def __init__(self, block_hash=SNAPSHOT_HASH):
        self.block_hash = block_hash
        self.get_block_hash_calls = []

    def get_block_hash(self, block):
        self.get_block_hash_calls.append(block)
        return self.block_hash

    def get_chain_head(self):
        return HEAD_HASH


def _backend(client, substrate=None, metagraph=None):
    subtensor = SimpleNamespace(
        substrate=substrate or FakeSubstrate(),
        metagraph=lambda netuid, block, lite: metagraph,
    )
    wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address='hk-self'))
    return ContractBackend(client, subtensor, wallet, netuid=74)


class TestSnapshotReads:
    def test_fetch_baskets_pins_all_reads_to_snapshot_hash(self):
        client = FakeClient(repos=[(1, 'a/b')], baskets={'hk1': [(1, 65535)]})
        substrate = FakeSubstrate()
        backend = _backend(client, substrate)
        assert backend.fetch_baskets(3600) == {'hk1': {'a/b': 65535}}
        assert substrate.get_block_hash_calls == [3600]
        assert set(client.read_ats) == {SNAPSHOT_HASH}

    def test_fetch_baskets_drops_deregistered_ids_and_empty_baskets(self):
        client = FakeClient(
            repos=[(1, 'a/b')],
            baskets={'hk1': [(1, 60000), (99, 5535)], 'hk2': [(99, 65535)]},
        )
        assert _backend(client).fetch_baskets(3600) == {'hk1': {'a/b': 60000}}

    def test_fetch_registry_pinned(self):
        client = FakeClient(repos=[(1, 'a/b'), (2, 'c/d')])
        substrate = FakeSubstrate()
        assert _backend(client, substrate).fetch_registry(7200) == {1: 'a/b', 2: 'c/d'}
        assert substrate.get_block_hash_calls == [7200]
        assert client.read_ats == [SNAPSHOT_HASH]

    def test_missing_block_hash_raises(self):
        backend = _backend(FakeClient(repos=[]), FakeSubstrate(block_hash=None))
        with pytest.raises(RuntimeError):
            backend.fetch_baskets(3600)

    def test_fetch_stakes_and_permits_from_metagraph(self):
        metagraph = SimpleNamespace(hotkeys=['hk1', 'hk2'], S=[3.0, 1.5], validator_permit=[True, False])
        stakes, permits = _backend(FakeClient(repos=[]), metagraph=metagraph).fetch_stakes_and_permits(3600)
        assert stakes == {'hk1': 3_000_000_000, 'hk2': 1_500_000_000}
        assert permits == {'hk1': True, 'hk2': False}


class TestOwnBasket:
    def test_resolved_at_head(self):
        client = FakeClient(repos=[(1, 'a/b'), (2, 'c/d')], own_basket=[(2, 5535), (1, 60000)])
        assert _backend(client).fetch_own_basket() == {'a/b': 60000, 'c/d': 5535}
        assert set(client.read_ats) == {HEAD_HASH}

    def test_none_when_no_basket(self):
        assert _backend(FakeClient(repos=[(1, 'a/b')])).fetch_own_basket() is None


class TestPublishBasket:
    def test_publishes_entries_sorted_by_id(self):
        client = FakeClient(repos=[(7, 'c/d'), (3, 'a/b')])
        backend = _backend(client)
        assert backend.publish_basket({'c/d': 5535, 'a/b': 60000}) is True
        entries, wallet = client.set_basket_calls[0]
        assert entries == [(3, 60000), (7, 5535)]
        assert wallet.hotkey.ss58_address == 'hk-self'

    def test_skips_tx_when_onchain_entries_match(self):
        client = FakeClient(repos=[(3, 'a/b'), (7, 'c/d')], own_basket=[(7, 5535), (3, 60000)])
        assert _backend(client).publish_basket({'a/b': 60000, 'c/d': 5535}) is True
        assert client.set_basket_calls == []

    def test_filters_unregistered_names_and_requantizes(self):
        client = FakeClient(repos=[(3, 'a/b'), (7, 'c/d')])
        backend = _backend(client)
        assert backend.publish_basket({'a/b': 30000, 'c/d': 30000, 'x/gone': 5535}) is True
        entries, _ = client.set_basket_calls[0]
        assert sorted(gid for gid, _ in entries) == [3, 7]
        assert sum(w for _, w in entries) == 65535

    def test_all_unregistered_returns_false_without_tx(self):
        client = FakeClient(repos=[(3, 'a/b')])
        assert _backend(client).publish_basket({'x/gone': 65535}) is False
        assert client.set_basket_calls == []
