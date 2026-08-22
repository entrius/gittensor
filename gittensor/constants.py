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
    'tex',
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
PR_LOOKBACK_DAYS = 30  # rolling window for scoring (per-repo default, overridable in the scoring config)
MERGED_PR_BASE_SCORE = 25  # cap on the quality term of base_score
MAX_CONTRIBUTION_BONUS = 5  # cap on the cross-category contribution bonus
CONTRIBUTION_SCORE_FOR_FULL_BONUS = 1500

# base_score = MERGED_PR_BASE_SCORE * (1 - exp(-src_tok / SRC_TOK_SATURATION_SCALE))
#            + min(total_score / CONTRIBUTION_SCORE_FOR_FULL_BONUS, 1) * MAX_CONTRIBUTION_BONUS
# SRC_TOK_SATURATION_SCALE: src_tok at ~63% of the cap; per-repo overridable
SRC_TOK_SATURATION_SCALE = 58.0

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

# Wall-clock budget (s) for the isolated-subprocess scoring path.
SCORING_SUBPROCESS_BUDGET_S = 10.0

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
    'rs': re.compile(r'^[ \t]*(?:#\[(?:cfg\()?test\b|#!\[cfg\(test\)\]|#\[\w+::test\b)', re.MULTILINE),
    'zig': re.compile(r'^[ \t]*test\b[ \t]*[{"]', re.MULTILINE),
    'd': re.compile(r'^[ \t]*unittest\b', re.MULTILINE),
}

# =============================================================================
# Eligibility Gate (OSS Contributions)
# =============================================================================
# Per-repo defaults — each repo may override these in master_repositories.json.
MIN_VALID_MERGED_PRS = 3  # minimum merged PRs (per repo) to receive score
MIN_CREDIBILITY = 0.80  # minimum credibility ratio to receive score

# =============================================================================
# Issue Discovery
# =============================================================================
# Eligibility gate — per-repo defaults, overridable in master_repositories.json.
MIN_VALID_SOLVED_ISSUES = 3  # minimum solved issues where solving PR has token_score >= MIN_TOKEN_SCORE_FOR_VALID_ISSUE
MIN_ISSUE_CREDIBILITY = 0.80  # minimum issue credibility ratio
MIN_TOKEN_SCORE_FOR_VALID_ISSUE = 5  # solving-PR token_score for a solved issue to count as "valid"

# Open issue spam threshold (per-repo: counts a repo's own open issues)
OPEN_ISSUE_SPAM_BASE_THRESHOLD = 2
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
# Pools (OSS + SERVING) must sum to 1.0; anything unallocated within them recycles to RECYCLE_UID.
OSS_EMISSION_SHARE = 1.00
DEFAULT_ISSUE_DISCOVERY_SHARE = 0.5
EMISSION_SHARE_TOLERANCE = 1e-9
MAX_MAINTAINER_CUT = 0.5  # maintaining is only half of the problem to software, at maximum

# =============================================================================
# Serving (sub-subnet B beta)
# =============================================================================
# Share of emissions paid to inference-serving miners, pro-rata by serving score.
# 0.0 = shadow mode: scores are computed and logged but nothing is paid. When
# raising above 0, shrink OSS_EMISSION_SHARE so all pools still sum to 1.0.
SERVING_EMISSION_SHARE = 0.0
SERVING_CHALLENGES_PER_ROUND = 4  # audit prompts sent to each serving miner per scoring round
SERVING_CHALLENGE_TIMEOUT = 30.0  # seconds before an audit counts as failed
# Latency credit for a 64-token audit (release.max_tokens). An honest 5090 answers in ~165 ms on-box
# (docs/serving-experiments/2026-08-22-planted-cheater: p95 166 ms); add validator<->miner RTT and an
# honest miner anywhere on earth lands under ~450 ms. Credit is flat to FULL and falls linearly to 0 at
# ZERO, so a miner proxying audits to a GPU in another region (extra RTT + a slower runtime: llama.cpp
# measured ~600 ms) loses credit in proportion. Same-region proxying is NOT visible here; that is the
# one-GPU-many-hotkeys case, caught by concurrent burst audits (launch plan, gap 0).
SERVING_LATENCY_FULL_CREDIT_MS = 500.0
SERVING_LATENCY_ZERO_CREDIT_MS = 1_500.0
# Per-audit tolerance bands. TELEMETRY ONLY: against sparkinfer 1b8b962 an honest miner meets them on just
# ~37% of audits (docs/serving-experiments/2026-08-22-planted-cheater), so they never decide pay or READY.
SERVING_AUDIT_MIN_PREFIX_AGREEMENT = 0.80  # fraction of reference greedy tokens reproduced before first divergence
SERVING_AUDIT_MAX_MEAN_ABS_LOGPROB_DIFF = 0.50  # mean |logprob delta| over the agreed prefix
# What decides: the mean positional overlap (fraction of positions whose token matches the reference) over the
# last SERVING_AUDIT_WINDOW audits of a (miner, release). A miner passes when that mean is >= the threshold for
# the number of audits it has so far (linear interpolation between rows). Thresholds are the 1% false-positive
# quantile of an honest miner's k-audit mean, bootstrapped from the experiment above; the window fills over
# SERVING_AUDIT_WINDOW / SERVING_CHALLENGES_PER_ROUND rounds and the bar rises with it. Recalibrate per pin:
#   python scripts/serving_cheat_experiment.py analyze --honest <honest rows> --cheaters <cheater rows>
SERVING_AUDIT_WINDOW = 20
SERVING_AUDIT_OVERLAP_THRESHOLDS = ((1, 0.016), (3, 0.143), (5, 0.232), (10, 0.339), (20, 0.415))
SERVING_API_DEFAULT_PORT = 8790
SERVING_MAX_TOKENS = 1024  # hard cap per request (API and miner both enforce)
SERVING_REQUEST_LOG_SIZE = 5_000  # in-memory ring of recent API/audit requests (telemetry)

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
# Excessive open PRs penalty (per-repo: counts a repo's own open PRs)
# Multiplier = 1.0 if open PRs <= threshold, 0.0 otherwise
EXCESSIVE_PR_PENALTY_BASE_THRESHOLD = 2

# Dynamic open PR threshold bonus for top contributors
# Bonus = floor(total_token_score / 300)
# Example: 900 total token score / 300 = +3 bonus
OPEN_PR_THRESHOLD_TOKEN_SCORE = 300.0  # Token score per +1 bonus
MAX_OPEN_PR_THRESHOLD = 30  # Maximum open PR threshold (base + bonus capped at this value)
