# Entrius 2025

# if a language/file extension isn't covered in our mapping, default to this.
# intuition is if we haven't covered it, it's more probable to be an unimportant language
DEFAULT_PROGRAMMING_LANGUAGE_WEIGHT = 0.12

BASE_GITHUB_API_URL = 'https://api.github.com'

# Github requirements
MIN_GITHUB_ACCOUNT_AGE = 180

# Scoring constants
MAX_ISSUES_SCORED_IN_SINGLE_PR = 3
UNIQUE_PR_BOOST = 0.6

# Time decay constants
TIME_DECAY_MIN_MULTIPLIER = 0.1  # Oldest PRs (at lookback window edge) get 10% of their score

# Rewards & Burning constants
PARETO_DISTRIBUTION_ALPHA_VALUE = 0.85
BURN_UID = 0

LINES_CONTRIBUTED_MAX_BURN = 0.9
LINES_CONTRIBUTED_BURN_DECAY_RATE = 0.00001

UNIQUE_PRS_MAX_BURN = 0.9
UNIQUE_PRS_BURN_DECAY_RATE = 0.005

# file types for which we want to mitigate rewards b/c of exploiting/gameability
MITIGATED_EXTENSIONS = ["md", "txt", "json"]
MAX_LINES_SCORED_CHANGES = 300

# PR spam mitigation constants - basically for every open pr above threshold, linearly decrease weight multiplier to final score (before pareto and normalization)
# Only applies to open prs to supported repositories.
EXCESSIVE_PR_PENALTY_THRESHOLD = 20
EXCESSIVE_PR_PENALTY_SLOPE = 0.05
EXCESSIVE_PR_MIN_WEIGHT = 0.01
