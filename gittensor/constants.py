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
# Language & File Scoring
# =============================================================================
DEFAULT_PROGRAMMING_LANGUAGE_WEIGHT = 0.12
TEST_FILE_CONTRIBUTION_WEIGHT = 0.05
# Extensions that use line-count scoring (capped at MAX_LINES_SCORED_FOR_NON_CODE_EXT)
# These are documentation, config, data files, or template languages without tree-sitter support
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
    'erb',
]
MAX_LINES_SCORED_FOR_NON_CODE_EXT = 300

# =============================================================================
# Repository & PR Scoring
# =============================================================================
PR_LOOKBACK_DAYS = 90  # how many days a merged pr will count for scoring
DEFAULT_MERGED_PR_BASE_SCORE = 30
MIN_TOKEN_SCORE_FOR_BASE_SCORE = 5  # PRs below this get 0 base score (can still earn contribution bonus)
MAX_CONTRIBUTION_BONUS = 30
DEFAULT_MAX_CONTRIBUTION_SCORE_FOR_FULL_BONUS = 2000

# Boosts
UNIQUE_PR_BOOST = 0.74
MAX_CODE_DENSITY_MULTIPLIER = 3.0

# Issue boosts
MAX_ISSUE_CLOSE_WINDOW_DAYS = 1
MAX_ISSUE_AGE_FOR_MAX_SCORE = 40  # days

# Time decay (sigmoid curve)
TIME_DECAY_GRACE_PERIOD_HOURS = 12  # hours before time decay begins
TIME_DECAY_SIGMOID_MIDPOINT = 10  # days until 50% score loss
TIME_DECAY_SIGMOID_STEEPNESS_SCALAR = 0.4
TIME_DECAY_MIN_MULTIPLIER = 0.05  # 5% of score will retain through lookback days (90D)

# Tree-sitter AST walk: max recursion depth to avoid stack overflow on pathological input
MAX_AST_DEPTH = 2000

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

# Tier-based emission allocation splits
TIER_EMISSION_SPLITS = {
    'Bronze': 0.15,  # 15% of emissions
    'Silver': 0.35,  # 35% of emissions
    'Gold': 0.50,  # 50% of emissions
}

# =============================================================================
# Rewards & Emissions
# =============================================================================
RECYCLE_UID = 0

# Network emission scaling (unique repos)
UNIQUE_REPOS_MAX_RECYCLE = 0.8
UNIQUE_REPOS_RECYCLE_DECAY_RATE = 0.005

# Network emission scaling (total token score from tiered miners)
TOKEN_SCORE_MAX_RECYCLE = 0.8
TOKEN_SCORE_RECYCLE_DECAY_RATE = 0.000012

# =============================================================================
# Spam & Gaming Mitigation
# =============================================================================
MAINTAINER_ASSOCIATIONS = ['OWNER', 'MEMBER', 'COLLABORATOR']

# Issue multiplier bonuses
MAX_ISSUE_AGE_BONUS = 0.75  # Max bonus for issue age (scales with sqrt of days open)
MAINTAINER_ISSUE_BONUS = 0.25  # Extra bonus when issue was created by a maintainer
# Excessive open PRs penalty
# Multiplier = 1.0 if open PRs <= threshold, 0.0 otherwise
EXCESSIVE_PR_PENALTY_BASE_THRESHOLD = 10

# Dynamic open PR threshold bonus for top contributors
# Bonus = floor(total_unlocked_token_score / 500)
# Example: 1500 token score across unlocked tiers / 500 = +3 bonus
OPEN_PR_THRESHOLD_TOKEN_SCORE = 500.0  # Token score per +1 bonus (sum of all unlocked tiers)
MAX_OPEN_PR_THRESHOLD = 30  # Maximum open PR threshold (base + bonus capped at this value)

# =============================================================================
# Issues Competition
# =============================================================================
CONTRACT_ADDRESS = '5FWNdk8YNtNcHKrAx2krqenFrFAZG7vmsd2XN2isJSew3MrD'
ISSUES_TREASURY_UID = 111  # UID of the smart contract neuron, if set to RECYCLE_UID then it's disabled
ISSUES_TREASURY_EMISSION_SHARE = 0.15  # % of emissions routed to funding issues treasury
