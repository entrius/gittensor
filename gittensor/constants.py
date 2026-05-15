# Entrius 2025
import re
from typing import Dict

# =============================================================================
# General
# =============================================================================
SECONDS_PER_DAY = 86400
SECONDS_PER_HOUR = 3600

# =============================================================================
# Network
# =============================================================================
NETWORK_MAP = {
    'finney': 'wss://entrypoint-finney.opentensor.ai:443',
    'test': 'wss://test.finney.opentensor.ai:443',
    'local': 'ws://127.0.0.1:9944',
}

# =============================================================================
# GitHub API
# =============================================================================
BASE_GITHUB_API_URL = 'https://api.github.com'
GITHUB_HTTP_TIMEOUT_SECONDS = 15
GRAPHQL_VIEWER_QUERY = '{ viewer { login } }'
# 1MB max file size for github api file fetches. Files exceeding this get no score.
MAX_FILE_SIZE_BYTES = 1_000_000
# Too many object lookups in one GraphQL query can trigger 502 errors and lose all results.
MAX_FILES_PER_GRAPHQL_BATCH = 50

# =============================================================================
# das-github-mirror (https://mirror.gittensor.io)
# =============================================================================
GITTENSOR_MIRROR_DEFAULT_URL = 'https://mirror.gittensor.io'
# File endpoint returns head/base blob contents; allow more time than plain GitHub calls.
MIRROR_HTTP_TIMEOUT_SECONDS = 30
MIRROR_MAX_ATTEMPTS = 3

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
EXTENSIONLESS_FILE_EXTENSIONS = {'dockerfile', 'makefile'}

# =============================================================================
# Repository & PR Scoring
# =============================================================================
PR_LOOKBACK_DAYS = 35  # rolling window for scoring
MERGED_PR_BASE_SCORE = 25
MIN_TOKEN_SCORE_FOR_BASE_SCORE = 5  # PRs below this get 0 base score
MAX_CONTRIBUTION_BONUS = 25
CONTRIBUTION_SCORE_FOR_FULL_BONUS = 1500

# Boosts
MAX_CODE_DENSITY_MULTIPLIER = 1.15

# Issue boosts
MAX_ISSUE_CLOSE_WINDOW_DAYS = 1

# Time decay (sigmoid curve)
TIME_DECAY_GRACE_PERIOD_HOURS = 12  # hours before time decay begins
TIME_DECAY_SIGMOID_MIDPOINT = 10  # days until 50% score loss
TIME_DECAY_SIGMOID_STEEPNESS_SCALAR = 0.4
TIME_DECAY_MIN_MULTIPLIER = 0.05  # 5% of score will retain through lookback window

# Per-parse CPU budget for tree-sitter. The parser polls this flag in its
# error-recovery loops; without it, adversarial inputs can spin forever in C
# while holding the GIL. 2s is well above the millisecond cost of real files.
TREE_SITTER_PARSE_TIMEOUT_MICROS = 2_000_000

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
# Eligibility Gate (OSS Contributions)
# =============================================================================
MIN_VALID_MERGED_PRS = 5  # minimum "valid" merged PRs (token_score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE) to receive score
MIN_CREDIBILITY = 0.80  # minimum credibility ratio to receive score
CREDIBILITY_MULLIGAN_COUNT = 1  # number of closed PRs forgiven (erased from merged+closed counts entirely)

# =============================================================================
# Issue Discovery
# =============================================================================
# Eligibility gate (stricter than OSS contributions)
MIN_VALID_SOLVED_ISSUES = 7  # minimum solved issues where solving PR has token_score >= MIN_TOKEN_SCORE_FOR_BASE_SCORE
MIN_ISSUE_CREDIBILITY = 0.80  # minimum issue credibility ratio

# Review quality cliff model (different from OSS: has clean bonus + steeper penalty)
ISSUE_REVIEW_CLEAN_BONUS = 1.1  # multiplier when 0 CHANGES_REQUESTED rounds
ISSUE_REVIEW_PENALTY_RATE = 0.15  # per CHANGES_REQUESTED round after cliff

# Open issue spam threshold
OPEN_ISSUE_SPAM_BASE_THRESHOLD = 5  # half the PR base of 10
OPEN_ISSUE_SPAM_TOKEN_SCORE_PER_SLOT = 300.0  # +1 allowed open issue per this much token score
MAX_OPEN_ISSUE_THRESHOLD = 30

# =============================================================================
# Collateral
# =============================================================================
OPEN_PR_COLLATERAL_PERCENT = 0.20

# =============================================================================
# Rewards & Emissions
# =============================================================================
RECYCLE_UID = 0

# Combined scoring pool distributed by repository emission_share, then by per-repo PR/issue split.
OSS_EMISSION_SHARE = 0.90
DEFAULT_ISSUE_DISCOVERY_SHARE = 0.5
EMISSION_SHARE_TOLERANCE = 1e-9

# =============================================================================
# Spam & Gaming Mitigation
# =============================================================================
MAINTAINER_ASSOCIATIONS = ['OWNER', 'MEMBER', 'COLLABORATOR']

# PR Review Quality Multiplier
REVIEW_PENALTY_RATE = 0.15  # 15% deduction per CHANGES_REQUESTED review from a maintainer
MAX_OPEN_PR_REVIEW_COLLATERAL_MULTIPLIER = 2.0  # Cap open PR collateral growth from review iterations

# Issue multiplier (flat values, no age scaling)
STANDARD_ISSUE_MULTIPLIER = 1.33  # Non-maintainer issue author
MAINTAINER_ISSUE_MULTIPLIER = 1.66  # Issue author is OWNER/MEMBER/COLLABORATOR
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
ISSUES_TREASURY_EMISSION_SHARE = 0.10  # % of emissions allocated to funding issues treasury
MAX_ISSUE_ID = 1_000_000  # sanity-check upper bound for any real deployment
