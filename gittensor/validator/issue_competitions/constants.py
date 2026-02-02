# The MIT License (MIT)
# Copyright 2025 Entrius

"""Constants for the Issue Bounties sub-mechanism"""

import json
import os
from pathlib import Path
from typing import Optional

# =============================================================================
# Sub-Subnet Emission Split
# =============================================================================

# Percentage of emissions allocated to issue bounties
ISSUES_EMISSION_WEIGHT = 0.5

# Percentage of emissions allocated to OSS contributions
OSS_EMISSION_WEIGHT = 0.5

# =============================================================================
# Contract Addresses
# =============================================================================

# Mainnet contract address (update after deployment)
ISSUE_CONTRACT_ADDRESS_MAINNET = ""

# Testnet contract address (update after deployment)
ISSUE_CONTRACT_ADDRESS_TESTNET = ""

# =============================================================================
# Feature Flags
# =============================================================================


def _get_issue_bounties_enabled() -> bool:
    """
    Check if issue bounties are enabled.

    Priority:
    1. ISSUE_BOUNTIES_ENABLED environment variable
    2. issue_bounties_enabled from ~/.gittensor/contract_config.json
    3. Default: False

    Returns:
        True if issue bounties should be enabled
    """
    # 1. Environment variable (highest priority)
    env_val = os.environ.get('ISSUE_BOUNTIES_ENABLED')
    if env_val is not None:
        return env_val.lower() == 'true'

    # Also check legacy name
    env_val = os.environ.get('ISSUE_COMPETITIONS_ENABLED')
    if env_val is not None:
        return env_val.lower() == 'true'

    # 2. Config file
    config_path = Path.home() / '.gittensor' / 'contract_config.json'
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            enabled = config.get('issue_bounties_enabled', config.get('issue_competitions_enabled'))
            if enabled is not None:
                return bool(enabled)
        except (json.JSONDecodeError, IOError):
            pass

    # 3. Default
    return False


# Enable/disable issue bounties - checks env var and config file
ISSUE_BOUNTIES_ENABLED = _get_issue_bounties_enabled()

# Contract UID for emissions routing (-1 = disabled)
ISSUES_CONTRACT_UID = int(os.environ.get('ISSUES_CONTRACT_UID', '-1'))

# Fixed emission rate for contract when enabled
ISSUES_FIXED_EMISSION_RATE = float(os.environ.get('ISSUES_FIXED_EMISSION_RATE', '0.1'))

# =============================================================================
# Block Timing
# =============================================================================

# Block time in seconds (Bittensor)
BLOCK_TIME_SECONDS = 12


# =============================================================================
# Contract Address Resolution
# =============================================================================

def get_contract_address(network: str = 'local') -> Optional[str]:
    """
    Get the contract address from environment, config file, or defaults.

    Priority:
    1. CONTRACT_ADDRESS environment variable (highest priority)
    2. ~/.gittensor/contract_config.json (written by dev-environment up.sh)
    3. Network-specific default (mainnet or testnet)

    Args:
        network: Network name ('mainnet', 'testnet', or 'local')

    Returns:
        Contract address string or None if not configured
    """
    # 1. Environment variable (highest priority)
    env_addr = os.environ.get('CONTRACT_ADDRESS')
    if env_addr:
        return env_addr

    # 2. Config file (written by up.sh during development)
    config_path = Path.home() / '.gittensor' / 'contract_config.json'
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            addr = config.get('contract_address')
            if addr:
                return addr
        except (json.JSONDecodeError, IOError):
            pass

    # 3. Network-specific default
    if network == 'mainnet':
        return ISSUE_CONTRACT_ADDRESS_MAINNET if ISSUE_CONTRACT_ADDRESS_MAINNET else None
    elif network == 'testnet':
        return ISSUE_CONTRACT_ADDRESS_TESTNET if ISSUE_CONTRACT_ADDRESS_TESTNET else None

    # No default for local network - must be configured
    return None


def get_ws_endpoint(network: str = 'local') -> str:
    """
    Get the WebSocket endpoint for the specified network.

    Priority:
    1. WS_ENDPOINT environment variable
    2. ~/.gittensor/contract_config.json
    3. Network-specific default

    Args:
        network: Network name ('mainnet', 'testnet', or 'local')

    Returns:
        WebSocket endpoint URL
    """
    # 1. Environment variable
    env_ws = os.environ.get('WS_ENDPOINT')
    if env_ws:
        return env_ws

    # 2. Config file
    config_path = Path.home() / '.gittensor' / 'contract_config.json'
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            ws = config.get('ws_endpoint')
            if ws:
                return ws
        except (json.JSONDecodeError, IOError):
            pass

    # 3. Network-specific default
    endpoints = {
        'mainnet': 'wss://entrypoint-finney.opentensor.ai:443',
        'testnet': 'wss://test.finney.opentensor.ai:443',
        'local': 'ws://127.0.0.1:9944',
    }
    return endpoints.get(network, endpoints['local'])
