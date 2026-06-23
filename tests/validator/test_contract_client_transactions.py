# Entrius 2025

"""Tests for IssueCompetitionContractClient transaction methods."""

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gittensor.validator.issue_competitions.contract_client import (
    DEFAULT_GAS_LIMIT,
    IssueCompetitionContractClient,
)

# (method, call_kwargs, expected_contract_method, expected_args, uses_hotkey, explicit_gas)
METHOD_TABLE = [
    (
        'vote_solution',
        lambda w: dict(issue_id=1, solver_hotkey='5Solver', solver_coldkey='5Cold', pr_number=42, wallet=w),
        'vote_solution',
        {'issue_id': 1, 'solver_hotkey': '5Solver', 'solver_coldkey': '5Cold', 'pr_number': 42},
        True,
        False,
    ),
    (
        'vote_cancel_issue',
        lambda w: dict(issue_id=2, reason='stale', wallet=w),
        'vote_cancel_issue',
        {'issue_id': 2, 'reason_hash': hashlib.sha256(b'stale').digest()},
        True,
        False,
    ),
    ('cancel_issue', lambda w: dict(issue_id=3, wallet=w), 'cancel_issue', {'issue_id': 3}, False, True),
    (
        'set_owner',
        lambda w: dict(new_owner='5NewOwner', wallet=w),
        'set_owner',
        {'new_owner': '5NewOwner'},
        False,
        True,
    ),
    ('add_validator', lambda w: dict(hotkey='5Val', wallet=w), 'add_validator', {'hotkey': '5Val'}, False, True),
    ('remove_validator', lambda w: dict(hotkey='5Val', wallet=w), 'remove_validator', {'hotkey': '5Val'}, False, True),
    (
        'set_treasury_hotkey',
        lambda w: dict(new_hotkey='5Treasury', wallet=w),
        'set_treasury_hotkey',
        {'new_hotkey': '5Treasury'},
        False,
        True,
    ),
]
_IDS = [row[0] for row in METHOD_TABLE]


@pytest.fixture()
def client():
    with patch.object(IssueCompetitionContractClient, '__init__', lambda self, *_args, **_kwargs: None):
        c = IssueCompetitionContractClient.__new__(IssueCompetitionContractClient)
        c.contract_address = '5FakeContract'
        c.subtensor = MagicMock()
        return c


@pytest.fixture()
def wallet():
    w = MagicMock()
    w.hotkey = SimpleNamespace(ss58_address='5HotkeyFake')
    w.coldkey = SimpleNamespace(ss58_address='5ColdkeyFake')
    return w


@pytest.mark.parametrize(
    'method, kwargs_fn, contract_method, expected_args, uses_hotkey, has_gas', METHOD_TABLE, ids=_IDS
)
def test_success_returns_true(client, wallet, method, kwargs_fn, contract_method, expected_args, uses_hotkey, has_gas):
    with patch.object(client, '_exec_contract_raw', return_value=('0xdeadbeef', None)) as mock:
        assert getattr(client, method)(**kwargs_fn(wallet)) is True
    mock.assert_called_once()
    kw = mock.call_args.kwargs
    assert kw['method_name'] == contract_method
    assert kw['args'] == expected_args
    assert kw['keypair'] is (wallet.hotkey if uses_hotkey else wallet.coldkey)
    assert kw['gas_limit'] == (DEFAULT_GAS_LIMIT if has_gas else None)


@pytest.mark.parametrize('method, kwargs_fn, _cm, _ea, _hk, _gas', METHOD_TABLE, ids=_IDS)
def test_failure_returns_false(client, wallet, method, kwargs_fn, _cm, _ea, _hk, _gas):
    with patch.object(client, '_exec_contract_raw', return_value=(None, 'submission failed')):
        assert getattr(client, method)(**kwargs_fn(wallet)) is False


@pytest.mark.parametrize('method, kwargs_fn, _cm, _ea, _hk, _gas', METHOD_TABLE, ids=_IDS)
def test_revert_returns_false(client, wallet, method, kwargs_fn, _cm, _ea, _hk, _gas):
    """Revert (hash + error) must be treated as failure"""
    with patch.object(client, '_exec_contract_raw', return_value=('0xdeadbeef', 'ContractReverted')):
        assert getattr(client, method)(**kwargs_fn(wallet)) is False


@pytest.mark.parametrize('method, kwargs_fn, _cm, _ea, _hk, _gas', METHOD_TABLE, ids=_IDS)
def test_exception_returns_false(client, wallet, method, kwargs_fn, _cm, _ea, _hk, _gas):
    with patch.object(client, '_exec_contract_raw', side_effect=RuntimeError('node down')):
        assert getattr(client, method)(**kwargs_fn(wallet)) is False


def _packed_treasury_storage():
    return SimpleNamespace(owner=b'\x01' * 32, treasury_hotkey=b'\x02' * 32, netuid=42)


def test_get_treasury_stake_raises_when_packed_storage_unavailable(client):
    with patch(
        'gittensor.validator.issue_competitions.contract_client.read_contract_packed_storage',
        return_value=None,
    ):
        with pytest.raises(RuntimeError, match='packed storage unavailable'):
            client.get_treasury_stake()


def test_get_treasury_stake_raises_when_alpha_query_fails(client):
    substrate = client.subtensor.substrate
    substrate.ss58_encode.side_effect = lambda value: f'ss58-{value}'
    substrate.query.side_effect = ConnectionResetError('alpha query reset')

    with patch(
        'gittensor.validator.issue_competitions.contract_client.read_contract_packed_storage',
        return_value=_packed_treasury_storage(),
    ):
        with pytest.raises(ConnectionResetError, match='alpha query reset'):
            client.get_treasury_stake()


def test_get_treasury_stake_returns_zero_for_empty_alpha_result(client):
    substrate = client.subtensor.substrate
    substrate.ss58_encode.side_effect = lambda value: f'ss58-{value}'
    substrate.query.return_value = None

    with patch(
        'gittensor.validator.issue_competitions.contract_client.read_contract_packed_storage',
        return_value=_packed_treasury_storage(),
    ):
        assert client.get_treasury_stake() == 0


# ---------------------------------------------------------------------------
# Regression tests for _encode_args str support (issue #1375)
# ---------------------------------------------------------------------------

class TestEncodeArgsStrType:
    """_encode_args must SCALE-encode str arguments as compact-length + UTF-8."""

    @pytest.fixture()
    def client(self):
        with patch.object(IssueCompetitionContractClient, '__init__', lambda self, *_args, **_kwargs: None):
            c = IssueCompetitionContractClient.__new__(IssueCompetitionContractClient)
            c.subtensor = MagicMock()
            return c

    def test_register_issue_does_not_raise(self, client):
        """register_issue with valid str args should encode without raising."""
        result = client._encode_args(
            'register_issue',
            {
                'github_url': 'https://github.com/owner/repo/issues/1',
                'repository_full_name': 'owner/repo',
                'issue_number': 1,
                'target_bounty': 10_000_000_000,
            },
        )
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_short_str_compact_prefix(self, client):
        """Strings < 64 bytes use single-byte compact prefix (n << 2)."""
        url = 'https://github.com/a/b/issues/1'
        utf8 = url.encode('utf-8')
        n = len(utf8)
        assert n < 64
        result = client._encode_args(
            'register_issue',
            {
                'github_url': url,
                'repository_full_name': 'a/b',
                'issue_number': 1,
                'target_bounty': 0,
            },
        )
        # First byte of result is compact-length for github_url
        assert result[0] == (n << 2)
        assert result[1 : 1 + n] == utf8

    def test_long_str_two_byte_compact_prefix(self, client):
        """Strings >= 64 bytes use two-byte compact prefix."""
        url = 'https://github.com/' + 'a' * 60 + '/issues/99999'
        utf8 = url.encode('utf-8')
        n = len(utf8)
        assert n >= 64
        result = client._encode_args(
            'register_issue',
            {
                'github_url': url,
                'repository_full_name': 'owner/repo',
                'issue_number': 1,
                'target_bounty': 0,
            },
        )
        # Two-byte compact: first byte has mode bits 0b01
        assert (result[0] & 0x03) == 1
        decoded_len = (result[0] >> 2) | (result[1] << 6)
        assert decoded_len == n

    def test_non_str_value_raises(self, client):
        """Passing a non-str value for a str arg should raise ValueError."""
        with pytest.raises(ValueError, match='Expected str'):
            client._encode_args(
                'register_issue',
                {
                    'github_url': 12345,  # wrong type
                    'repository_full_name': 'owner/repo',
                    'issue_number': 1,
                    'target_bounty': 0,
                },
            )
