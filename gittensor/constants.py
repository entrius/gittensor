# Entrius 2025
import re
from typing import Dict

# =============================================================================
# General
# =============================================================================
SECONDS_PER_DAY = 86400
SECONDS_PER_HOUR = 3600

# =============================================================================
# GitHub API
# =============================================================================
BASE_GITHUB_API_URL = 'https://api.github.com'
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
PR_LOOKBACK_DAYS = 35  # rolling window for scoring
MERGED_PR_BASE_SCORE = 30
MIN_TOKEN_SCORE_FOR_BASE_SCORE = 5  # PRs below this get 0 base score (can still earn contribution bonus)
MAX_CONTRIBUTION_BONUS = 30
CONTRIBUTION_SCORE_FOR_FULL_BONUS = 2000

# Boosts
MAX_CODE_DENSITY_MULTIPLIER = 3.0

# Pioneer dividend — rewards the first quality contributor to each repository
# Rates applied per follower position (1st follower pays most, diminishing after)
# Dividend capped at PIONEER_DIVIDEND_MAX_RATIO × pioneer's own earned_score
PIONEER_DIVIDEND_RATE_1ST = 0.30  # 1st follower: 30% of their earned_score
PIONEER_DIVIDEND_RATE_2ND = 0.20  # 2nd follower: 20% of their earned_score
PIONEER_DIVIDEND_RATE_REST = 0.10  # 3rd+ followers: 10% of their earned_score
PIONEER_DIVIDEND_MAX_RATIO = 1.0  # Cap dividend at 1× pioneer's own earned_score (max 2× total)

# Issue boosts
MAX_ISSUE_CLOSE_WINDOW_DAYS = 1
MAX_ISSUE_AGE_FOR_MAX_SCORE = 40  # days

# Time decay (sigmoid curve)
TIME_DECAY_GRACE_PERIOD_HOURS = 12  # hours before time decay begins
TIME_DECAY_SIGMOID_MIDPOINT = 10  # days until 50% score loss
TIME_DECAY_SIGMOID_STEEPNESS_SCALAR = 0.4
TIME_DECAY_MIN_MULTIPLIER = 0.05  # 5% of score will retain through lookback window

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

# Extensions where source files may contain inline test blocks (e.g. Rust #[cfg(test)], Zig test declarations)
INLINE_TEST_EXTENSIONS = frozenset({'rs', 'zig', 'd'})

INLINE_TEST_PATTERNS: Dict[str, re.Pattern] = {
    'rs': re.compile(r'^\s*(?:#\[(?:cfg\()?test\b|#!\[cfg\(test\)\]|#\[\w+::test\b)', re.MULTILINE),
    'zig': re.compile(r'^\s*test\b\s*[{"]', re.MULTILINE),
    'd': re.compile(r'^\s*unittest\b', re.MULTILINE),
}

# =============================================================================
# Eligibility Gate
# =============================================================================
MIN_VALID_MERGED_PRS = 5  # minimum "valid" merged PRs (token_score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE) to receive score
MIN_CREDIBILITY = 0.75  # minimum credibility ratio to receive score
CREDIBILITY_MULLIGAN_COUNT = 1  # number of closed PRs forgiven (erased from merged+closed counts entirely)

# =============================================================================
# Collateral
# =============================================================================
OPEN_PR_COLLATERAL_PERCENT = 0.20

# =============================================================================
# Rewards & Emissions
# =============================================================================
RECYCLE_UID = 0

# Network emission scaling (unique repos)
UNIQUE_REPOS_MAX_RECYCLE = 0.8
UNIQUE_REPOS_RECYCLE_DECAY_RATE = 0.005

# Network emission scaling (total token score from eligible miners)
TOKEN_SCORE_MAX_RECYCLE = 0.8
TOKEN_SCORE_RECYCLE_DECAY_RATE = 0.000012

# =============================================================================
# Spam & Gaming Mitigation
# =============================================================================
MAINTAINER_ASSOCIATIONS = ['OWNER', 'MEMBER', 'COLLABORATOR']

# PR Review Quality Multiplier
REVIEW_PENALTY_RATE = 0.12  # 12% deduction per CHANGES_REQUESTED review from a maintainer

# Issue multiplier bonuses
MAX_ISSUE_AGE_BONUS = 0.75  # Max bonus for issue age (scales with sqrt of days open)
MAINTAINER_ISSUE_BONUS = 0.25  # Extra bonus when issue was created by a maintainer
# Excessive open PRs penalty
# Multiplier = 1.0 if open PRs <= threshold, 0.0 otherwise
EXCESSIVE_PR_PENALTY_BASE_THRESHOLD = 10

# Dynamic open PR threshold bonus for top contributors
# Bonus = floor(total_token_score / 300)
# Example: 900 total token score / 300 = +3 bonus
OPEN_PR_THRESHOLD_TOKEN_SCORE = 300.0  # Token score per +1 bonus
MAX_OPEN_PR_THRESHOLD = 30  # Maximum open PR threshold (base + bonus capped at this value)

# =============================================================================
# Issues Competition
# =============================================================================
CONTRACT_ADDRESS = '5FWNdk8YNtNcHKrAx2krqenFrFAZG7vmsd2XN2isJSew3MrD'
ISSUES_TREASURY_UID = 111  # UID of the smart contract neuron, if set to RECYCLE_UID then it's disabled
ISSUES_TREASURY_EMISSION_SHARE = 0.15  # % of emissions allocated to funding issues treasury
