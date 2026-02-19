#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Unit tests for CLI helper functions.
"""

from unittest.mock import Mock, patch
import pytest

from gittensor.cli.issue_commands.helpers import (
    format_alpha,
    validate_ss58,
    validate_repo_format,
    verify_github_repo,
    verify_github_issue
)

def test_format_alpha():
    assert format_alpha(1000000000) == "1.0000 ALPHA"
    assert format_alpha(500000000) == "0.5000 ALPHA"
    assert format_alpha(10500000000) == "10.5000 ALPHA"
    assert format_alpha(0) == "0.0000 ALPHA"

def test_validate_ss58():
    # Valid Bittensor addresses
    assert validate_ss58("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY") is True
    assert validate_ss58("5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty") is True
    
    # Invalid addresses
    assert validate_ss58("invalid") is False
    assert validate_ss58("") is False
    assert validate_ss58("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQ") is False # too short
    assert validate_ss58("1" * 48) is False # Valid length but probably invalid checksum/format

def test_validate_repo_format():
    assert validate_repo_format("opentensor/btcli") is True
    assert validate_repo_format("tensorflow/tensorflow") is True
    assert validate_repo_format("a/b") is True
    
    # Invalid formats
    assert validate_repo_format("repo") is False
    assert validate_repo_format("owner/repo/extra") is False
    assert validate_repo_format("/repo") is False
    assert validate_repo_format("owner/") is False
    assert validate_repo_format("owner / repo") is False
    assert validate_repo_format("owner/ repo") is False
    assert validate_repo_format("owner /repo") is False
    assert validate_repo_format("") is False

@patch('requests.get')
def test_verify_github_repo(mock_get):
    # Success
    mock_get.return_value = Mock(status_code=200)
    assert verify_github_repo("owner/repo") is True
    
    # Not found
    mock_get.return_value = Mock(status_code=404)
    assert verify_github_repo("owner/repo") is False
    
    # Error
    mock_get.side_effect = Exception("Connection error")
    assert verify_github_repo("owner/repo") is False

@patch('requests.get')
def test_verify_github_issue(mock_get):
    # Success - Issue
    mock_response = Mock(status_code=200)
    mock_response.json.return_value = {
        'state': 'open',
        'title': 'Test Issue'
    }
    mock_get.return_value = mock_response
    
    result = verify_github_issue("owner/repo", 1)
    assert result['exists'] is True
    assert result['is_pull_request'] is False
    assert result['state'] == 'open'
    assert result['title'] == 'Test Issue'
    
    # Success - PR
    mock_response.json.return_value = {
        'state': 'open',
        'title': 'Test PR',
        'pull_request': {}
    }
    result = verify_github_issue("owner/repo", 2)
    assert result['exists'] is True
    assert result['is_pull_request'] is True
    
    # Not found
    mock_get.side_effect = None
    mock_get.return_value = Mock(status_code=404)
    assert verify_github_issue("owner/repo", 3) == {}
    
    # Error
    mock_get.side_effect = Exception("API error")
    assert verify_github_issue("owner/repo", 4) == {}
