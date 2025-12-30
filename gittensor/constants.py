from datetime import datetime, timezone

# Entrius 2025
# =============================================================================
# General
# =============================================================================
SECONDS_PER_DAY = 86400
SECONDS_PER_HOUR = 3600

# =============================================================================
# GitHub API
# =============================================================================
BASE_GITHUB_API_URL = "https://api.github.com"
MIN_GITHUB_ACCOUNT_AGE = 180  # days

# =============================================================================
# Gittensor Branding
# =============================================================================
PR_TAGLINE_PREFIX = "Contribution by Gittensor, see my contribution statistics at "
GITTENSOR_MINER_DETAILS_URL = "https://gittensor.io/miners/details?githubId="

# =============================================================================
# Language & File Scoring
# =============================================================================
DEFAULT_PROGRAMMING_LANGUAGE_WEIGHT = 0.12
TEST_FILE_CONTRIBUTION_WEIGHT = 0.05
MITIGATED_EXTENSIONS = ["md", "txt", "json"]
MAX_LINES_SCORED_FOR_MITIGATED_EXT = 300

# =============================================================================
# Repository & PR Scoring
# =============================================================================
DEFAULT_MERGED_PR_BASE_SCORE = 50

# Boosts
UNIQUE_PR_BOOST = 0.4

# Issue boosts
MAX_ISSUE_CLOSE_WINDOW_DAYS = 1
MAX_ISSUE_AGE_FOR_MAX_SCORE = 45  # days

# Time decay (sigmoid curve)
TIME_DECAY_GRACE_PERIOD_HOURS = 12  # hours before time decay begins
TIME_DECAY_SIGMOID_MIDPOINT = 10  # days until 50% score loss
TIME_DECAY_SIGMOID_STEEPNESS_SCALAR = 0.4
TIME_DECAY_MIN_MULTIPLIER = 0.05  # 5% of score will retain through lookback days (90D)

# =============================================================================
# Tiers & Collateral System
# =============================================================================
TIER_BASED_INCENTIVE_MECHANISM_START_DATE = datetime(2025, 12, 27, 17, 00, 00, tzinfo=timezone.utc)
DEFAULT_COLLATERAL_PERCENT = 0.20

MAX_LINE_CONTRIBUTION_BONUS = 30
DEFAULT_MAX_CONTRIBUTION_SCORE_FOR_FULL_BONUS = 2000  # For reference: 2000 score = 1,000 python lines

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
# Spam & Gaming Mitigation
# =============================================================================
MAINTAINER_ASSOCIATIONS = ['OWNER', 'MEMBER', 'COLLABORATOR']

# Issue multiplier bonuses
MAX_ISSUE_AGE_BONUS = 0.75  # Max bonus for issue age (scales with sqrt of days open)
MAINTAINER_ISSUE_BONUS = 0.25  # Extra bonus when issue was created by a maintainer

# Typo detection (for filtering non-scoreable lines)
TYPO_MAX_DIST = 2
TYPO_MIN_SIM = 0.75

# Excessive open PRs penalty
EXCESSIVE_PR_PENALTY_THRESHOLD = 10
EXCESSIVE_PR_PENALTY_SLOPE = 0.50
EXCESSIVE_PR_MIN_MULTIPLIER = 0.00

COMMENT_PATTERNS = [
    r'^\s*#',  # Python, Ruby, Shell, etc.
    r'^\s*//',  # C, C++, Java, JavaScript, Go, Rust, etc.
    r'^\s*/\*',  # C-style multi-line start
    r'^\s*\*',  # C-style multi-line continuation
    r'^\s*\*/',  # C-style multi-line end
    r'^\s*--',  # SQL, Lua, Haskell
    r'^\s*<!--',  # HTML, XML
    r'^\s*%',  # LaTeX, MATLAB
    r'^\s*;',  # Lisp, Assembly
    r'^\s*"""',  # Python docstring
    r"^\s*'''",  # Python docstring
]

PREPROCESSOR_LANGUAGES = {
    'c',
    'h',
    'cpp',
    'cxx',
    'cc',
    'hpp',
    'hxx',
    'hh',
    'h++',
    'cs',
    'rs',
    'swift',
}
