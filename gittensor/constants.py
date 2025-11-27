# Entrius 2025

# =============================================================================
# General
# =============================================================================
SECONDS_PER_DAY = 86400

# =============================================================================
# GitHub API
# =============================================================================
BASE_GITHUB_API_URL = "https://api.github.com"
MIN_GITHUB_ACCOUNT_AGE = 180  # days

# =============================================================================
# Gittensor Branding
# =============================================================================
PR_TAGLINE = "Contribution by Gittensor, learn more at https://gittensor.io/"

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
UNIQUE_PR_BOOST = 0.25

# Time decay (sigmoid curve)
TIME_DECAY_SIGMOID_MIDPOINT = 4  # days until 50% score loss
TIME_DECAY_SIGMOID_STEEPNESS_SCALAR = 0.9
TIME_DECAY_MIN_MULTIPLIER = 0.005

# =============================================================================
# Spam & Gaming Mitigation
# =============================================================================
# Typo detection
TYPO_RATIO_THRESHOLD = 0.8
TYPO_ONLY_PENALTY_MULTIPLIER = 0.01
TYPO_MAX_DIST = 2
TYPO_MIN_SIM = 0.75
MAX_TYPO_FILE_PATCH_LINES = 20

# Excessive open PRs penalty
EXCESSIVE_PR_PENALTY_THRESHOLD = 12
EXCESSIVE_PR_PENALTY_SLOPE = 0.08333
EXCESSIVE_PR_MIN_WEIGHT = 0.01

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