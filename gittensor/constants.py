# Entrius 2025
from datetime import datetime, timezone

# =============================================================================
# General
# =============================================================================
SECONDS_PER_DAY = 86400
SECONDS_PER_HOUR = 3600

# =============================================================================
# Temp Vars
# =============================================================================
CREDIBILITY_APPLICATION_DATE = datetime(2025, 12, 4, 18, 0, 0, tzinfo=timezone.utc)
CREDIBILITY_THRESHOLD = 2 # Credibility is effective after  attempts.

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
# Issue Scoring
# =============================================================================
MAX_ISSUE_CLOSE_WINDOW_DAYS = 1
MAX_ISSUE_AGE_FOR_MAX_SCORE = 45  # days

# =============================================================================
# Repository & PR Scoring
# =============================================================================
UNIQUE_PR_BOOST = 0.4

# Time decay (sigmoid curve)
TIME_DECAY_GRACE_PERIOD_HOURS = 4  # hours before time decay begins
TIME_DECAY_SIGMOID_MIDPOINT = 4  # days until 50% score loss
TIME_DECAY_SIGMOID_STEEPNESS_SCALAR = 0.9
TIME_DECAY_MIN_MULTIPLIER = 0.01

# =============================================================================
# Spam & Gaming Mitigation
# =============================================================================
IGNORED_AUTHOR_ASSOCIATIONS = ['OWNER', 'MEMBER', 'COLLABORATOR']

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

# =============================================================================
# Tiers & Collateral System
# =============================================================================
# Date when collateral system becomes effective (PRs created after this date are subject to collateral)
TIERS_AND_COLLATERAL_EFFECTIVE_DATE = datetime(2025, 12, 25, 22, 0, 0, tzinfo=timezone.utc)
# Percentage of potential score used as collateral for open PRs
DEFAULT_COLLATERAL_PERCENT = 0.20
DEFAULT_MERGED_PR_BASE_SCORE = 50
MAX_CONTRIBUTION_BONUS_SCORE = 20
DEFAULT_MAX_CONTRIBUTION_SCORE_FOR_FULL_BONUS = 2000 # For reference: 2000 score = 1,000 python lines

# =============================================================================
# Rewards & Emissions
# =============================================================================
RECYCLE_UID = 0

# Network emission scaling (lines contributed)
LINES_CONTRIBUTED_MAX_RECYCLE = 0.9
LINES_CONTRIBUTED_RECYCLE_DECAY_RATE = 0.000005

# Network emission scaling (total merged prs)
MERGED_PRS_MAX_RECYCLE = 0.9
MERGED_PRS_RECYCLE_DECAY_RATE = 0.0015

# Network emission scaling (unique PRs)
UNIQUE_PRS_MAX_RECYCLE = 0.9
UNIQUE_PRS_RECYCLE_DECAY_RATE = 0.006
