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
PR_TAGLINE = "Contribution by Gittensor, learn more at https://gittensor.io/"
GITTENSOR_TAGLINE_BOOST = 2.0 # PRs with the Gittensor Tagline will receive 2x the score

# =============================================================================
# Language & File Scoring
# =============================================================================
DEFAULT_PROGRAMMING_LANGUAGE_WEIGHT = 0.12
TEST_FILE_CONTRIBUTION_WEIGHT = 0.25
MITIGATED_EXTENSIONS = ["md", "txt", "json"]
MAX_LINES_SCORED_FOR_MITIGATED_EXT = 300

# =============================================================================
# Issue Scoring
# =============================================================================
MAX_ISSUES_SCORED_IN_SINGLE_PR = 3
MAX_ISSUE_CLOSE_WINDOW_DAYS = 5
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
# Typo detection (for filtering non-scoreable lines)
TYPO_MAX_DIST = 2
TYPO_MIN_SIM = 0.75

# Excessive open PRs penalty
EXCESSIVE_PR_PENALTY_THRESHOLD = 8
EXCESSIVE_PR_PENALTY_SLOPE = 0.50
EXCESSIVE_PR_MIN_MULTIPLIER = 0.00

COMMENT_PATTERNS = [
    r'^\s*#',           # Python, Ruby, Shell, etc.
    r'^\s*//',          # C, C++, Java, JavaScript, Go, Rust, etc.
    r'^\s*/\*',         # C-style multi-line start
    r'^\s*\*',          # C-style multi-line continuation
    r'^\s*\*/',         # C-style multi-line end
    r'^\s*--',          # SQL, Lua, Haskell
    r'^\s*<!--',        # HTML, XML
    r'^\s*%',           # LaTeX, MATLAB
    r'^\s*;',           # Lisp, Assembly
    r'^\s*"""',         # Python docstring
    r"^\s*'''",         # Python docstring
]

# =============================================================================
# Rewards & Emissions
# =============================================================================
RECYCLE_UID = 0
PARETO_DISTRIBUTION_ALPHA_VALUE = 0.85

# Network emission scaling (lines contributed)
LINES_CONTRIBUTED_MAX_RECYCLE = 0.9
LINES_CONTRIBUTED_RECYCLE_DECAY_RATE = 0.00001

# Network emission scaling (unique PRs)
UNIQUE_PRS_MAX_RECYCLE = 0.9
UNIQUE_PRS_RECYCLE_DECAY_RATE = 0.02