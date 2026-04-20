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
    with patch.object(IssueCompetitionContractClient, '__init__', lambda self, *a, **kw: None):
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
    with patch.object(client, '_exec_contract_raw', return_value='0xdeadbeef') as mock:
        assert getattr(client, method)(**kwargs_fn(wallet)) is True
    mock.assert_called_once()
    kw = mock.call_args.kwargs
    assert kw['method_name'] == contract_method
    assert kw['args'] == expected_args
    assert kw['keypair'] is (wallet.hotkey if uses_hotkey else wallet.coldkey)
    assert kw['gas_limit'] == (DEFAULT_GAS_LIMIT if has_gas else None)


@pytest.mark.parametrize('method, kwargs_fn, _cm, _ea, _hk, _gas', METHOD_TABLE, ids=_IDS)
def test_failure_returns_false(client, wallet, method, kwargs_fn, _cm, _ea, _hk, _gas):
    with patch.object(client, '_exec_contract_raw', return_value=None):
        assert getattr(client, method)(**kwargs_fn(wallet)) is False


@pytest.mark.parametrize('method, kwargs_fn, _cm, _ea, _hk, _gas', METHOD_TABLE, ids=_IDS)
def test_exception_returns_false(client, wallet, method, kwargs_fn, _cm, _ea, _hk, _gas):
    with patch.object(client, '_exec_contract_raw', side_effect=RuntimeError('node down')):
        assert getattr(client, method)(**kwargs_fn(wallet)) is False
