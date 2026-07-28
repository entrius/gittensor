# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared fakes for repo-registry CLI tests."""

from types import SimpleNamespace
from typing import Optional

from gittensor.validator.repo_registry.storage_utils import (
    PackedRegistryStorage,
    ParamBounds,
    RegistryConstants,
)

HEAD_HASH = '0xhead'
_UNSET = object()


def make_repo(github_id: int, full_name: str, owner: str = '5RepoOwner', reg_block: int = 10, active: bool = True):
    return SimpleNamespace(github_id=github_id, full_name=full_name, owner=owner, reg_block=reg_block, active=active)


def make_constants(**overrides) -> RegistryConstants:
    values = dict(
        max_repos=32,
        immunity_period=216_000,
        price_floor=500,
        price_ceiling=500_000,
        price_half_life=100_800,
        price_bump_q32=1 << 33,
        max_regs_per_block=1,
        param_rate_limit_blocks=3600,
        snapshot_interval=3600,
        basket_cap=10,
    )
    values.update(overrides)
    return RegistryConstants(**values)


def make_packed(constants: Optional[RegistryConstants] = None) -> PackedRegistryStorage:
    return PackedRegistryStorage(
        owner=b'\x01' * 32,
        paused=False,
        netuid=74,
        storage_version=1,
        price_last=1_000,
        price_last_block=5,
        last_reg_block=5,
        regs_in_block=0,
        constants=constants or make_constants(),
        repo_ids=[],
        voters=[],
        bound_keys=list(range(1, 27)),
    )


class FakeRegistryClient:
    """Recording fake for RepoRegistryContractClient."""

    def __init__(
        self,
        repos=(),
        params=None,
        labels=None,
        patterns=None,
        baskets=None,
        bounds=None,
        packed=_UNSET,
        quote=None,
        tx_result=('0xdeadbeef', None),
        set_basket_result=True,
        clear_basket_result=True,
    ):
        self.repos = list(repos)
        self.params = params or {}
        self.labels = labels or {}
        self.patterns = patterns or {}
        self.baskets = baskets or {}
        self.bounds = bounds or {}
        self.packed = make_packed() if packed is _UNSET else packed
        self.quote = quote
        self.tx_result = tx_result
        self.set_basket_result = set_basket_result
        self.clear_basket_result = clear_basket_result
        self.calls = []

    # Reads
    def get_registry(self, at=None):
        return self.packed

    def get_all_repos(self, at=None):
        return [repo for repo in self.repos if repo.active]

    def get_repo(self, github_id, at=None):
        return next((repo for repo in self.repos if repo.github_id == github_id), None)

    def get_params(self, github_id, at=None):
        return self.params.get(github_id, {})

    def get_bounds(self, key, at=None):
        raw = self.bounds.get(key)
        return ParamBounds(*raw) if raw else None

    def get_label_multipliers(self, github_id, at=None):
        return self.labels.get(github_id, {})

    def get_branch_patterns(self, github_id, at=None):
        return self.patterns.get(github_id, [])

    def get_basket(self, hotkey, at=None):
        return self.baskets.get(hotkey)

    def get_all_baskets(self, at=None):
        return dict(self.baskets)

    def quote_price(self, at=None):
        return self.quote

    # Writes (all recorded)
    def _record(self, name, *args):
        self.calls.append((name, *args))
        return self.tx_result

    def register(self, github_id, full_name, fee_hotkey, wallet):
        return self._record('register', github_id, full_name, fee_hotkey)

    def set_param(self, github_id, key, value, wallet):
        return self._record('set_param', github_id, key, value)

    def set_label_multiplier(self, github_id, label, value, wallet):
        return self._record('set_label_multiplier', github_id, label, value)

    def remove_label_multiplier(self, github_id, label, wallet):
        return self._record('remove_label_multiplier', github_id, label)

    def set_branch_patterns(self, github_id, patterns, wallet):
        return self._record('set_branch_patterns', github_id, patterns)

    def update_full_name(self, github_id, full_name, wallet):
        return self._record('update_full_name', github_id, full_name)

    def transfer_ownership(self, github_id, new_owner, wallet):
        return self._record('transfer_ownership', github_id, new_owner)

    def deregister(self, github_id, wallet):
        return self._record('deregister', github_id)

    def set_basket(self, entries, wallet):
        from gittensor.validator.repo_registry.contract_client import validate_basket_entries

        validate_basket_entries(entries)
        self.calls.append(('set_basket', entries))
        return self.set_basket_result

    def clear_basket(self, wallet):
        self.calls.append(('clear_basket',))
        return self.clear_basket_result


def make_wallet(hotkey_ss58: str = '5FakeHotkey', coldkey_ss58: str = '5FakeColdkey'):
    return SimpleNamespace(
        hotkey=SimpleNamespace(ss58_address=hotkey_ss58),
        coldkey=SimpleNamespace(ss58_address=coldkey_ss58),
    )


def make_subtensor(block: int = 100):
    substrate = SimpleNamespace(get_chain_head=lambda: HEAD_HASH)
    return SimpleNamespace(get_current_block=lambda: block, substrate=substrate)


RESOLVED = ('5FakeContractAddr', 'ws://fake', 'test')
