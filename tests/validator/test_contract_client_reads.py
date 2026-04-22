# Entrius 2025

"""Tests for IssueCompetitionContractClient numeric read methods."""

from unittest.mock import MagicMock, patch

import pytest

from gittensor.validator.issue_competitions.contract_client import (
    IssueCompetitionContractClient,
)


@pytest.fixture()
def client():
    with patch.object(IssueCompetitionContractClient, '__init__', lambda self, *a, **kw: None):
        c = IssueCompetitionContractClient.__new__(IssueCompetitionContractClient)
        c.contract_address = '5FakeContract'
        c.subtensor = MagicMock()
        return c


def test_read_contract_numeric_returns_zero_when_response_is_none(client):
    extractor = MagicMock()
    with patch.object(client, '_raw_contract_read', return_value=None):
        assert client._read_contract_numeric('any', extractor) == 0
    extractor.assert_not_called()


def test_read_contract_numeric_returns_zero_when_extractor_returns_none(client):
    with patch.object(client, '_raw_contract_read', return_value=b'\x00'):
        assert client._read_contract_numeric('any', lambda _b: None) == 0


def test_read_contract_numeric_returns_extracted_value(client):
    with patch.object(client, '_raw_contract_read', return_value=b'\xff' * 4):
        assert client._read_contract_numeric('any', lambda _b: 1234) == 1234


@pytest.mark.parametrize(
    'method, contract_method, extractor_attr',
    [
        ('get_alpha_pool', 'get_alpha_pool', '_extract_u128_from_response'),
        ('get_last_harvest_block', 'get_last_harvest_block', '_extract_u32_from_response'),
    ],
)
def test_public_read_wires_method_name_and_extractor(client, method, contract_method, extractor_attr):
    with patch.object(client, '_read_contract_numeric', return_value=42) as mock:
        assert getattr(client, method)() == 42
    name_arg, extractor_arg = mock.call_args.args
    assert name_arg == contract_method
    assert extractor_arg == getattr(client, extractor_attr)


@pytest.mark.parametrize('method', ['get_alpha_pool', 'get_last_harvest_block'])
def test_public_read_returns_zero_on_exception(client, method):
    with patch.object(client, '_read_contract_numeric', side_effect=RuntimeError('node down')):
        assert getattr(client, method)() == 0
