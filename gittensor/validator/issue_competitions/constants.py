# The MIT License (MIT)
# Copyright 2025 Entrius

"""Constants for the Issue Competitions sub-mechanism."""

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

# Enable/disable issue competitions (set True when ready for production)
ISSUE_COMPETITIONS_ENABLED = False

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
