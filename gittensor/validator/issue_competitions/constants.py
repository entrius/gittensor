# The MIT License (MIT)
# Copyright 2025 Entrius

"""Constants for the Issue Competitions sub-mechanism."""

import json
import os
from pathlib import Path
from typing import Optional

# =============================================================================
# ELO System Constants
# =============================================================================

# Initial ELO rating for new miners
INITIAL_ELO = 800

# K-factor for ELO calculation (higher = more volatile)
K_FACTOR = 40

# Minimum ELO to be eligible for competitions
ELO_CUTOFF = 700

# Number of days to look back for ELO calculation
LOOKBACK_DAYS = 30

# Daily decay factor for EMA (exponential moving average)
# Recent competitions weighted more heavily (0.9^days_ago)
EMA_DECAY_FACTOR = 0.9

# =============================================================================
# Sub-Subnet Emission Split
# =============================================================================

# Percentage of emissions allocated to issue competitions
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

# Enable/disable issue competitions via environment variable
ISSUE_COMPETITIONS_ENABLED = os.environ.get('ISSUE_COMPETITIONS_ENABLED', 'false').lower() == 'true'

# Contract UID for emissions routing (-1 = disabled)
ISSUES_CONTRACT_UID = int(os.environ.get('ISSUES_CONTRACT_UID', '-1'))

# Fixed emission rate for contract when enabled
ISSUES_FIXED_EMISSION_RATE = float(os.environ.get('ISSUES_FIXED_EMISSION_RATE', '0.1'))

# =============================================================================
# Competition Timing (mirrors smart contract defaults)
# =============================================================================

# Submission window in blocks (~2 days at 12s blocks)
DEFAULT_SUBMISSION_WINDOW_BLOCKS = 14400

# Competition deadline in blocks (~7 days at 12s blocks)
DEFAULT_COMPETITION_DEADLINE_BLOCKS = 50400

# Proposal expiry in blocks (~3.3 hours at 12s blocks)
DEFAULT_PROPOSAL_EXPIRY_BLOCKS = 1000

# Block time in seconds (Bittensor)
BLOCK_TIME_SECONDS = 12

# =============================================================================
# Issue Preferences
# =============================================================================

# Maximum number of issues a miner can express preference for
MAX_ISSUE_PREFERENCES = 5


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
