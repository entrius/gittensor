# Entrius 2025
from datetime import datetime, timezone

# =============================================================================
# General
# =============================================================================
SECONDS_PER_DAY = 86400
SECONDS_PER_HOUR = 3600

# =============================================================================
# GitHub API
# =============================================================================
BASE_GITHUB_API_URL = 'https://api.github.com'
MIN_GITHUB_ACCOUNT_AGE = 180  # days
# 1MB max file size for github api file fetches. Files exceeding this get no score.
MAX_FILE_SIZE_BYTES = 1_000_000

# =============================================================================
# Gittensor Branding
# =============================================================================
PR_TAGLINE_PREFIX = 'Contribution by Gittensor, see my contribution statistics at '
GITTENSOR_MINER_DETAILS_URL = 'https://gittensor.io/miners/details?githubId='

# =============================================================================
# Language & File Scoring
# =============================================================================
DEFAULT_PROGRAMMING_LANGUAGE_WEIGHT = 0.12
TEST_FILE_CONTRIBUTION_WEIGHT = 0.05
# Extensions that use line-count scoring (capped at MAX_LINES_SCORED_FOR_NON_CODE_EXT)
# These are documentation, config, or data files
NON_CODE_EXTENSIONS = [
    'md',
    'mdx',
    'markdown',
    'txt',
    'text',
    'rst',
    'adoc',
    'asciidoc',
    'json',
    'jsonc',
    'yaml',
    'yml',
    'toml',
    'xml',
    'csv',
    'tsv',
    'ini',
    'cfg',
    'conf',
    'config',
    'properties',
    'plist',
]
MAX_LINES_SCORED_FOR_NON_CODE_EXT = 300

# =============================================================================
# Repository & PR Scoring
# =============================================================================
DEFAULT_MERGED_PR_BASE_SCORE = 30
MIN_TOKEN_SCORE_FOR_BASE_SCORE = 5  # PRs below this get 0 base score (can still earn contribution bonus)
MAX_CONTRIBUTION_BONUS = 30
DEFAULT_MAX_CONTRIBUTION_SCORE_FOR_FULL_BONUS = 2000

# Boosts
UNIQUE_PR_BOOST = 0.4
MAX_CODE_DENSITY_MULTIPLIER = 3.0

# Issue boosts
MAX_ISSUE_CLOSE_WINDOW_DAYS = 1
MAX_ISSUE_AGE_FOR_MAX_SCORE = 40  # days

# Time decay (sigmoid curve)
TIME_DECAY_GRACE_PERIOD_HOURS = 12  # hours before time decay begins
TIME_DECAY_SIGMOID_MIDPOINT = 10  # days until 50% score loss
TIME_DECAY_SIGMOID_STEEPNESS_SCALAR = 0.4
TIME_DECAY_MIN_MULTIPLIER = 0.05  # 5% of score will retain through lookback days (90D)

# comment nodes for token scoring
COMMENT_NODE_TYPES = frozenset(
    {
        'comment',
        'line_comment',
        'block_comment',
        'documentation_comment',
        'doc_comment',
    }
)

# =============================================================================
# Tiers & Collateral System
# =============================================================================
TIER_BASED_INCENTIVE_MECHANISM_START_DATE = datetime(2025, 12, 31, 3, 45, 00, tzinfo=timezone.utc)
DEFAULT_COLLATERAL_PERCENT = 0.20

# =============================================================================
# Rewards & Emissions
# =============================================================================
RECYCLE_UID = 0

DEFAULT_FIXED_RECYCLE_RATE = 0.50
DYNAMIC_EMISSIONS_BUFFER_DAYS = 45  # After 45 days of launching tier based IM, we will restore dynamic emissions

# Network emission scaling (lines contributed)
LINES_CONTRIBUTED_MAX_RECYCLE = 0.9
LINES_CONTRIBUTED_RECYCLE_DECAY_RATE = 0.000005

# Network emission scaling (total merged prs)
MERGED_PRS_MAX_RECYCLE = 0.9
MERGED_PRS_RECYCLE_DECAY_RATE = 0.0015

# Network emission scaling (unique PRs)
UNIQUE_PRS_MAX_RECYCLE = 0.9
UNIQUE_PRS_RECYCLE_DECAY_RATE = 0.006

# =============================================================================
# Low-Value PR Detection (Tiered Thresholds)
# =============================================================================
# Smaller PRs have stricter thresholds
# Larger PRs are more lenient (naturally include config, docs, etc.).
# NOTE: This is deprecated at the moment. all values set to 0
LOW_VALUE_THRESHOLD_SMALL = 0.0  # 0.4
LOW_VALUE_THRESHOLD_MEDIUM = 0.0  # 0.35
LOW_VALUE_THRESHOLD_LARGE = 0.0  # 0.3
LOW_VALUE_SIZE_SMALL = 25  # Lines threshold for "small" PRs
LOW_VALUE_SIZE_MEDIUM = 125  # Lines threshold for "medium" PRs

# =============================================================================
# Spam & Gaming Mitigation
# =============================================================================
MAINTAINER_ASSOCIATIONS = ['OWNER', 'MEMBER', 'COLLABORATOR']

# Issue multiplier bonuses
MAX_ISSUE_AGE_BONUS = 0.75  # Max bonus for issue age (scales with sqrt of days open)
MAINTAINER_ISSUE_BONUS = 0.25  # Extra bonus when issue was created by a maintainer
# Excessive open PRs penalty
EXCESSIVE_PR_PENALTY_THRESHOLD = 10
EXCESSIVE_PR_PENALTY_SLOPE = 0.50
EXCESSIVE_PR_MIN_MULTIPLIER = 0.00
