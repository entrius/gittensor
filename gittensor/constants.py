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
OSS_EMISSION_SHARE = 0.50  # repo emission_share values are fractions of THIS pool (sparkinfer 0.4 -> 20% of total)
DEFAULT_ISSUE_DISCOVERY_SHARE = 0.5
EMISSION_SHARE_TOLERANCE = 1e-9
MAX_MAINTAINER_CUT = 0.5  # maintaining is only half of the problem to software, at maximum

# =============================================================================
# Serving (sub-subnet B beta)
# =============================================================================
# Share of emissions paid to inference-serving miners, pro-rata by serving score. With no miner
# passing audits the whole pool recycles to RECYCLE_UID, so this is safe to hold above 0 before
# miners exist. 0.0 would be shadow mode (scores computed and logged, nothing paid). Move
# OSS_EMISSION_SHARE in the same commit so the pools still sum to 1.0.
SERVING_EMISSION_SHARE = 0.50
assert abs(OSS_EMISSION_SHARE + SERVING_EMISSION_SHARE - 1.0) < EMISSION_SHARE_TOLERANCE, (
    'emission pools must sum to 1.0'
)
SERVING_CHALLENGES_PER_ROUND = 4  # audit prompts sent to each serving miner per scoring round
SERVING_CHALLENGE_TIMEOUT = 30.0  # seconds before an audit counts as failed
SERVING_AUDIT_CONCURRENCY = (
    256  # serving axons audited in parallel per round (sockets are cheap; dead axons hold a slot 30 s)
)
SERVING_READY_TTL_S = 900.0  # gateway stops routing when the last audit round is older than this
# Capacity probe: after the correctness audits, every miner whose window passed is sent SERVING_PROBE_REQUESTS audit
# prompts at the same instant as every other miner. Verified tokens delivered per wall-clock second, over
# SERVING_PROBE_TARGET_TPS, capped at 1, is the miner's capacity. One RTX 5090 delivers a fixed throughput however many
# hotkeys front it, so N hotkeys on one card share one card's pay; a hotkey with more than one card is capped at one
# card's worth (register one hotkey per GPU). Target = what one honest 5090 delivers under this probe as measured by the
# validator (RTT included). Calibrated on testnet 2026-08-25 (validator on a DO droplet, card in Romania): one miner alone
# 183-186 tok/s; two hotkeys on the same card 104-115 tok/s each (sum 210-227) -> capacities ~1.0 vs ~0.6.
# Probe outcomes affect capacity only, never the audit window (a slow host halves its pay; it does not flap out of READY).
SERVING_PROBE_REQUESTS = 6
SERVING_PROBE_TARGET_TPS = 180.0
# Latency credit for a 64-token audit (release.max_tokens). An honest 5090 answers in ~165 ms on-box
# (measured 2026-08-22/24: p95 166 ms); add validator<->miner RTT and an
# honest miner anywhere on earth lands under ~450 ms. Credit is flat to FULL and falls linearly to 0 at
# ZERO, so a miner proxying audits to a GPU in another region (extra RTT + a slower runtime: llama.cpp
# measured ~600 ms) loses credit in proportion. Same-region proxying is NOT visible here; that is the
# one-GPU-many-hotkeys case, caught by concurrent burst audits (launch plan, gap 0).
SERVING_LATENCY_FULL_CREDIT_MS = 500.0
SERVING_LATENCY_ZERO_CREDIT_MS = 1_500.0
# Per-audit verdict. The blessed runtime is bit-reproducible (sparkinfer 12954e6, SPARKINFER_DETERMINISTIC=1), so an
# honest miner reproduces the reference's greedy tokens exactly and its logprobs to float noise; every planted
# cheater differs on every prompt (measured 2026-08-24, internal serving-experiments notes: honest max |delta| 0.0000,
# cheapest cheater min mean |delta| 0.0057 / min max |delta| 0.129). Both bands must hold for an audit to pass.
SERVING_AUDIT_MIN_PREFIX_AGREEMENT = 1.0  # fraction of reference greedy tokens reproduced before first divergence
SERVING_AUDIT_MAX_MEAN_ABS_LOGPROB_DIFF = 0.005  # mean |logprob delta| over the agreed prefix
SERVING_AUDIT_MAX_ABS_LOGPROB_DIFF = 0.10  # largest single-position |logprob delta|
# Rolling window: the mean of per-audit outcomes (1 pass / 0 fail; misses and wrong model are 0) over the last
# SERVING_AUDIT_WINDOW audits of a (hotkey, release) must reach the threshold for the number of audits seen so
# far (rows are (k, threshold), linearly interpolated, flat beyond the last row). With a deterministic runtime the
# honest mean is 1.0, so the bar only leaves room for transient misses. At 4 audits per 5-minute round, one missed
# round (4/10 = 0.6 < 0.8) costs ~10 minutes out of the READY set; a single missed audit does not.
# History: the 1b8b962 pin needed a positional-overlap window with a calibrated ramp (0.016 at k=1 .. 0.415 at
# k=20; 2026-08-22 notes). Recalibrate with
#   python scripts/serving_cheat_experiment.py analyze --honest <honest rows> --cheaters <cheater rows>
SERVING_AUDIT_WINDOW = 10
SERVING_AUDIT_WINDOW_THRESHOLDS = ((1, 0.8),)
SERVING_API_DEFAULT_PORT = 8790
# Miner: a hotkey may query for inference only with at least this much stake on the subnet (alpha, metagraph.S). Same
# rule as allways: validators and builders with skin in the game get to use the product. Env SERVING_MIN_CALLER_STAKE.
SERVING_MIN_CALLER_STAKE = 250_000.0
SERVING_BACKEND_CONCURRENCY = 16  # miner: concurrent backend generations (sparkinfer >= 12954e6 handles 24)
SERVING_SEEN_NONCES = 10_000  # miner: replay guard size; covers many minutes of validator traffic
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
