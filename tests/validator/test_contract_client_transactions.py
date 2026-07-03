# Entrius 2025

"""Tests for IssueCompetitionContractClient transaction methods."""

import hashlib
import struct
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gittensor.validator.issue_competitions.contract_client import (
    DEFAULT_GAS_LIMIT,
    IssueCompetitionContractClient,
    _scale_compact_length,
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


class TestScaleCompactLength:
    """Boundary coverage for the SCALE compact-length encoder."""

    @pytest.mark.parametrize(
        'n, expected',
        [
            (0, b'\x00'),
            (1, b'\x04'),
            (63, bytes([63 << 2])),
        ],
    )
    def test_mode_0_single_byte(self, n, expected):
        assert _scale_compact_length(n) == expected

    @pytest.mark.parametrize('n', [64, 100, 16383])
    def test_mode_1_two_bytes(self, n):
        encoded = _scale_compact_length(n)
        assert len(encoded) == 2
        assert encoded == ((n << 2) | 1).to_bytes(2, 'little')

    @pytest.mark.parametrize('n', [16384, 100_000, (1 << 30) - 1])
    def test_mode_2_four_bytes(self, n):
        encoded = _scale_compact_length(n)
        assert len(encoded) == 4
        assert encoded == ((n << 2) | 2).to_bytes(4, 'little')

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match='non-negative'):
            _scale_compact_length(-1)

    def test_rejects_oversize(self):
        with pytest.raises(ValueError, match='too large'):
            _scale_compact_length(1 << 30)


class TestEncodeArgsStr:
    """SCALE encoding of `str` arguments via _encode_args (regression for #1374)."""

    def test_register_issue_short_url_encodes(self, client):
        url = 'https://github.com/owner/repo/issues/1'
        repo = 'owner/repo'
        url_bytes = url.encode('utf-8')
        repo_bytes = repo.encode('utf-8')
        assert len(url_bytes) < 64

        encoded = client._encode_args(
            'register_issue',
            {
                'github_url': url,
                'repository_full_name': repo,
                'issue_number': 1,
                'target_bounty': 10_000_000_000,
            },
        )

        offset = 0
        assert encoded[offset] == len(url_bytes) << 2
        offset += 1
        assert encoded[offset : offset + len(url_bytes)] == url_bytes
        offset += len(url_bytes)

        assert encoded[offset] == len(repo_bytes) << 2
        offset += 1
        assert encoded[offset : offset + len(repo_bytes)] == repo_bytes
        offset += len(repo_bytes)

        assert struct.unpack_from('<I', encoded, offset)[0] == 1
        offset += 4

        low = struct.unpack_from('<Q', encoded, offset)[0]
        high = struct.unpack_from('<Q', encoded, offset + 8)[0]
        assert low + (high << 64) == 10_000_000_000
        assert len(encoded) == offset + 16

    def test_register_issue_long_url_uses_mode_1(self, client):
        long_repo = 'a' * 30 + '/' + 'b' * 30
        url = f'https://github.com/{long_repo}/issues/12345'
        url_bytes = url.encode('utf-8')
        assert 64 <= len(url_bytes) < 16384

        encoded = client._encode_args(
            'register_issue',
            {
                'github_url': url,
                'repository_full_name': long_repo,
                'issue_number': 12345,
                'target_bounty': 1,
            },
        )

        expected_prefix = ((len(url_bytes) << 2) | 1).to_bytes(2, 'little')
        assert encoded[:2] == expected_prefix
        assert encoded[2 : 2 + len(url_bytes)] == url_bytes

    def test_register_issue_rejects_non_string_github_url(self, client):
        with pytest.raises(ValueError, match='Expected str for github_url'):
            client._encode_args(
                'register_issue',
                {
                    'github_url': 12345,
                    'repository_full_name': 'owner/repo',
                    'issue_number': 1,
                    'target_bounty': 1,
                },
            )

    def test_register_issue_unicode_url_roundtrips(self, client):
        url = 'https://github.com/Δοκιμή/π/issues/1'
        url_bytes = url.encode('utf-8')

        encoded = client._encode_args(
            'register_issue',
            {
                'github_url': url,
                'repository_full_name': 'owner/repo',
                'issue_number': 1,
                'target_bounty': 1,
            },
        )

        assert encoded[0] == len(url_bytes) << 2
        assert encoded[1 : 1 + len(url_bytes)] == url_bytes


class TestPayoutBounty:
    """`payout_bounty` must report a successful on-chain payout as success.

    The amount is read before the payout tx. When that pre-payout read fails
    transiently (get_issue swallows errors and returns None), a successful
    payout must not be reported to callers as a falsy failure — that would
    prompt a duplicate payout on an already-paid issue.
    """

    def test_success_returns_amount(self, client, wallet):
        with (
            patch.object(client, 'get_issue', return_value=SimpleNamespace(bounty_amount=7000)) as get_issue,
            patch.object(client, '_exec_contract_raw', return_value=('0xdeadbeef', None)),
        ):
            assert client.payout_bounty(3, wallet) == 7000
        get_issue.assert_called_once_with(3)

    def test_success_with_failed_preread_rereads_amount(self, client, wallet):
        # Pre-payout read fails (None), payout succeeds on-chain, re-read recovers
        # the amount — the result must be the truthy amount, not a falsy 0.
        with (
            patch.object(client, 'get_issue', side_effect=[None, SimpleNamespace(bounty_amount=5000)]) as get_issue,
            patch.object(client, '_exec_contract_raw', return_value=('0xdeadbeef', None)),
        ):
            result = client.payout_bounty(3, wallet)
        assert result == 5000
        assert bool(result) is True
        assert get_issue.call_count == 2

    def test_success_with_unreadable_amount_returns_zero(self, client, wallet):
        # Both reads fail but the tx succeeded — still not treated as a failure.
        with (
            patch.object(client, 'get_issue', return_value=None),
            patch.object(client, '_exec_contract_raw', return_value=('0xdeadbeef', None)),
        ):
            assert client.payout_bounty(3, wallet) == 0

    def test_submission_failure_returns_none(self, client, wallet):
        with (
            patch.object(client, 'get_issue', return_value=SimpleNamespace(bounty_amount=7000)),
            patch.object(client, '_exec_contract_raw', return_value=(None, 'submission failed')),
        ):
            assert client.payout_bounty(3, wallet) is None

    def test_revert_returns_none(self, client, wallet):
        with (
            patch.object(client, 'get_issue', return_value=SimpleNamespace(bounty_amount=7000)),
            patch.object(client, '_exec_contract_raw', return_value=('0xdeadbeef', 'ContractReverted')),
        ):
            assert client.payout_bounty(3, wallet) is None
