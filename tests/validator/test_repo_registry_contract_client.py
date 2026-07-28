# Entrius 2025

"""Tests for RepoRegistryContractClient reads and transaction methods."""

import struct
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gittensor.validator.issue_competitions.storage_utils import compute_ink5_lazy_key
from gittensor.validator.repo_registry.contract_client import (
    DEFAULT_GAS_LIMIT,
    REGISTER_GAS_LIMIT,
    RepoRegistryContractClient,
    validate_basket_entries,
)
from gittensor.validator.repo_registry.storage_utils import (
    BASKETS_ROOT_KEY,
    PACKED_ROOT_STORAGE_KEY,
    PARAM_BOUNDS_ROOT_KEY,
    PARAMS_ROOT_KEY,
    REPOS_ROOT_KEY,
    encode_compact_length,
)
from tests.utils.test_repo_registry_storage_utils import _build_packed, _build_repo

VOTER_A = b'\xaa' * 32
VOTER_B = b'\xbb' * 32


class _FakeContractInfo:
    def __init__(self, value):
        self.value = value


class _FakeSubstrate:
    """Serves childstate cells from a {storage_key: bytes} store."""

    def __init__(self, cells):
        self._cells = cells
        self.storage_requests = []

    def query(self, module, storage, params):
        assert (module, storage) == ('Contracts', 'ContractInfoOf')
        return _FakeContractInfo({'trie_id': '0x0102'})

    def rpc_request(self, method, params):
        assert method == 'childstate_getStorage'
        self.storage_requests.append(params)
        value = self._cells.get(params[1])
        return {'result': '0x' + value.hex() if value else None}

    def ss58_encode(self, account_hex):
        return f'ss58:{account_hex[:8]}'

    def ss58_decode(self, ss58_address):
        return {'ss58:aaaaaaaa': VOTER_A.hex(), 'ss58:bbbbbbbb': VOTER_B.hex()}[ss58_address]

    def get_block_number(self, block_hash):
        return 200


def _mapping_key(root_key, encoded_key):
    return compute_ink5_lazy_key(root_key, encoded_key)


def _make_client(cells):
    client = RepoRegistryContractClient.__new__(RepoRegistryContractClient)
    client.contract_address = '5FakeContract'
    substrate = _FakeSubstrate(cells)
    client.subtensor = SimpleNamespace(substrate=substrate, block=200)
    return client, substrate


@pytest.fixture()
def wallet():
    return SimpleNamespace(
        hotkey=SimpleNamespace(ss58_address='5HotkeyFake'),
        coldkey=SimpleNamespace(ss58_address='5ColdkeyFake'),
    )


# ============================================================================
# Reads
# ============================================================================


def test_get_registry_empty_contract_returns_none():
    client, _ = _make_client({})
    assert client.get_registry() is None
    assert client.get_all_repos() == []
    assert client.get_voters() == []
    assert client.get_all_baskets() == {}
    assert client.quote_price() is None


def test_get_all_repos_filters_inactive_and_missing():
    cells = {
        PACKED_ROOT_STORAGE_KEY: _build_packed(repo_ids=[1, 2, 3]),
        _mapping_key(REPOS_ROOT_KEY, struct.pack('<Q', 1)): _build_repo(github_id=1, full_name='a/one'),
        _mapping_key(REPOS_ROOT_KEY, struct.pack('<Q', 2)): _build_repo(github_id=2, full_name='a/two', active=False),
    }
    client, _ = _make_client(cells)

    repos = client.get_all_repos()

    assert [(repo.github_id, repo.full_name, repo.active) for repo in repos] == [(1, 'a/one', True)]
    assert repos[0].owner == 'ss58:07070707'


def test_get_all_repos_empty_registry():
    client, _ = _make_client({PACKED_ROOT_STORAGE_KEY: _build_packed()})
    assert client.get_all_repos() == []


def test_reads_pin_block_hash():
    cells = {
        PACKED_ROOT_STORAGE_KEY: _build_packed(repo_ids=[1]),
        _mapping_key(REPOS_ROOT_KEY, struct.pack('<Q', 1)): _build_repo(github_id=1),
    }
    client, substrate = _make_client(cells)

    client.get_all_repos(at='0xsnapshot')

    assert substrate.storage_requests
    assert all(params[2] == '0xsnapshot' for params in substrate.storage_requests)


def test_get_params_and_missing_default():
    entries = encode_compact_length(1) + struct.pack('<BQ', 4, 100_000)
    cells = {_mapping_key(PARAMS_ROOT_KEY, struct.pack('<Q', 42)): entries}
    client, _ = _make_client(cells)

    assert client.get_params(42) == {4: 100_000}
    assert client.get_params(99) == {}


def test_get_all_bounds_walks_bound_keys():
    cells = {
        PACKED_ROOT_STORAGE_KEY: _build_packed(bound_keys=[1, 7]),
        _mapping_key(PARAM_BOUNDS_ROOT_KEY, struct.pack('<B', 1)): struct.pack('<QQ', 0, 1_000_000),
        _mapping_key(PARAM_BOUNDS_ROOT_KEY, struct.pack('<B', 7)): struct.pack('<QQ', 300_000, 1_000_000),
    }
    client, _ = _make_client(cells)

    bounds = client.get_all_bounds()

    assert set(bounds) == {1, 7}
    assert (bounds[7].min, bounds[7].max) == (300_000, 1_000_000)
    assert client.get_bounds(99) is None


def test_get_all_baskets_walks_voters():
    basket_a = encode_compact_length(1) + struct.pack('<QH', 1, 65_535)
    basket_b = encode_compact_length(2) + struct.pack('<QH', 1, 30_000) + struct.pack('<QH', 2, 35_535)
    cells = {
        PACKED_ROOT_STORAGE_KEY: _build_packed(voters=[VOTER_A, VOTER_B]),
        _mapping_key(BASKETS_ROOT_KEY, VOTER_A): basket_a,
        _mapping_key(BASKETS_ROOT_KEY, VOTER_B): basket_b,
    }
    client, _ = _make_client(cells)

    baskets = client.get_all_baskets()

    assert baskets == {
        'ss58:aaaaaaaa': [(1, 65_535)],
        'ss58:bbbbbbbb': [(1, 30_000), (2, 35_535)],
    }


def test_get_basket_by_hotkey_and_missing():
    basket = encode_compact_length(1) + struct.pack('<QH', 7, 65_535)
    cells = {_mapping_key(BASKETS_ROOT_KEY, VOTER_A): basket}
    client, _ = _make_client(cells)

    assert client.get_basket('ss58:aaaaaaaa') == [(7, 65_535)]
    assert client.get_basket('ss58:bbbbbbbb') is None


def test_get_voters_encodes_ss58():
    client, _ = _make_client({PACKED_ROOT_STORAGE_KEY: _build_packed(voters=[VOTER_A])})
    assert client.get_voters() == ['ss58:aaaaaaaa']


def test_quote_price_uses_replica_with_pinned_block():
    # price_last decayed from block 100 to 200 stays clamped at the floor
    client, _ = _make_client({PACKED_ROOT_STORAGE_KEY: _build_packed(price_last=500_000_000_000, price_last_block=100)})
    assert client.quote_price(at='0xsnapshot') == 500_000_000_000
    assert client.quote_price() == 500_000_000_000


def test_quote_price_decays_from_ceiling():
    packed = _build_packed(price_last=500_000_000_000_000, price_last_block=199)
    client, _ = _make_client({PACKED_ROOT_STORAGE_KEY: packed})
    # delta = 1 block at launch constants — golden vector value
    assert client.quote_price() == 499_996_561_789_885


# ============================================================================
# Basket validation
# ============================================================================


def test_validate_basket_entries_accepts_valid():
    validate_basket_entries([(1, 30_000), (2, 35_535)])


@pytest.mark.parametrize(
    'entries, match',
    [
        ([], 'empty'),
        ([(1, 0), (2, 65_535)], 'Zero weight'),
        ([(1, 30_000), (1, 35_535)], 'Duplicate'),
        ([(1, 30_000), (2, 30_000)], 'sum to 65535'),
    ],
)
def test_validate_basket_entries_rejects_invalid(entries, match):
    with pytest.raises(ValueError, match=match):
        validate_basket_entries(entries)


# ============================================================================
# Transactions
# ============================================================================


def test_register_signs_with_coldkey(wallet):
    client, _ = _make_client({})
    with patch.object(client, '_exec_contract_raw', return_value=('0xdeadbeef', None)) as mock:
        result = client.register(github_id=42, full_name='a/one', fee_hotkey='5FeeHot', wallet=wallet)

    assert result == ('0xdeadbeef', None)
    kw = mock.call_args.kwargs
    assert kw['method_name'] == 'register'
    assert kw['args'] == {'github_id': 42, 'full_name': 'a/one', 'fee_hotkey': '5FeeHot'}
    assert kw['keypair'] is wallet.coldkey
    assert kw['gas_limit'] == REGISTER_GAS_LIMIT


def test_set_basket_signs_with_hotkey(wallet):
    client, _ = _make_client({})
    entries = [(1, 30_000), (2, 35_535)]
    with patch.object(client, '_exec_contract_raw', return_value=('0xdeadbeef', None)) as mock:
        assert client.set_basket(entries, wallet) is True

    kw = mock.call_args.kwargs
    assert kw['method_name'] == 'set_basket'
    assert kw['args'] == {'entries': entries}
    assert kw['keypair'] is wallet.hotkey
    assert kw['gas_limit'] == DEFAULT_GAS_LIMIT


def test_set_basket_rejects_invalid_entries_before_submission(wallet):
    client, _ = _make_client({})
    with patch.object(client, '_exec_contract_raw') as mock:
        with pytest.raises(ValueError, match='sum to 65535'):
            client.set_basket([(1, 1)], wallet)
    mock.assert_not_called()


def test_clear_basket_signs_with_hotkey(wallet):
    client, _ = _make_client({})
    with patch.object(client, '_exec_contract_raw', return_value=('0xdeadbeef', None)) as mock:
        assert client.clear_basket(wallet) is True

    kw = mock.call_args.kwargs
    assert kw['method_name'] == 'clear_basket'
    assert kw['args'] == {}
    assert kw['keypair'] is wallet.hotkey


@pytest.mark.parametrize('outcome', [(None, 'submission failed'), ('0xdeadbeef', 'ContractReverted')])
def test_basket_tx_failure_and_revert_return_false(wallet, outcome):
    client, _ = _make_client({})
    entries = [(1, 65_535)]
    with patch.object(client, '_exec_contract_raw', return_value=outcome):
        assert client.set_basket(entries, wallet) is False
        assert client.clear_basket(wallet) is False


def test_basket_tx_exception_returns_false(wallet):
    client, _ = _make_client({})
    with patch.object(client, '_exec_contract_raw', side_effect=RuntimeError('node down')):
        assert client.set_basket([(1, 65_535)], wallet) is False
        assert client.clear_basket(wallet) is False


# ============================================================================
# Argument encoding
# ============================================================================


def test_encode_args_register():
    client, _ = _make_client({})
    encoded = client._encode_args('register', {'github_id': 42, 'full_name': 'a/one', 'fee_hotkey': 'ss58:aaaaaaaa'})
    name = b'a/one'
    assert encoded == struct.pack('<Q', 42) + encode_compact_length(len(name)) + name + VOTER_A


def test_encode_args_set_basket():
    client, _ = _make_client({})
    encoded = client._encode_args('set_basket', {'entries': [(1, 30_000), (2, 35_535)]})
    assert encoded == encode_compact_length(2) + struct.pack('<QH', 1, 30_000) + struct.pack('<QH', 2, 35_535)


def test_encode_args_missing_argument_raises():
    client, _ = _make_client({})
    with pytest.raises(ValueError, match='Missing argument'):
        client._encode_args('register', {'github_id': 42})
