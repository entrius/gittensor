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
OSS_EMISSION_SHARE = 0.965  # repo emission_share values are fractions of THIS pool (sparkinfer 0.4 -> 37% of total)
DEFAULT_ISSUE_DISCOVERY_SHARE = 0.5
EMISSION_SHARE_TOLERANCE = 1e-9
MAX_MAINTAINER_CUT = 0.5  # maintaining is only half of the problem to software, at maximum

# =============================================================================
# Serving (sub-subnet B beta)
# =============================================================================
# Serving miners are paid an accounting price per verified GPU-hour, not a fixed slice: each READY card-equivalent
# (capacity-weighted, so N hotkeys on one card sum to one card) earns SERVING_GPU_HOUR_USD, converted to an emission
# share through the on-chain alpha/TAO price and the TAO/USD rate published in serving_loadout.json (`pricing`).
# The share is capped at SERVING_EMISSION_SHARE_CAP; what the fleet does not earn recycles to RECYCLE_UID, never to
# OSS. 2026-08-26 (2,950 alpha/day to miners, $0.85/alpha): $0.70/h funds ~5 cards inside the 3.5% cap. With no
# price data (testnet) the cap is paid pro-rata. Move OSS_EMISSION_SHARE in the same commit so the pools sum to 1.0.
SERVING_GPU_HOUR_USD = 0.70
SERVING_EMISSION_SHARE_CAP = 0.035
# Pricing the cap needs the chain's alpha/TAO price and the published TAO/USD rate. A round that cannot read them
# reuses the last usable pricing for up to SERVING_PRICING_MAX_AGE_S, so a chain hiccup does not move pay. With no
# usable pricing at all the fleet is paid nothing: the alternative (the whole cap, split pro-rata) hands one verified
# card 3.5% of emissions. A network with no price data to read - testnet - sets SERVING_PAY_CAP_WITHOUT_PRICING.
SERVING_PRICING_MAX_AGE_S = 3600.0
assert abs(OSS_EMISSION_SHARE + SERVING_EMISSION_SHARE_CAP - 1.0) < EMISSION_SHARE_TOLERANCE, (
    'emission pools must sum to 1.0'
)
# Settlement is over the trailing hour: a miner's serving score is the mean of its last SERVING_SETTLEMENT_ROUNDS
# round scores (missing rounds count 0), so a card verified for 45 of the last 60 minutes earns 75% of the price.
SERVING_SETTLEMENT_ROUNDS = 12  # 12 x 5-minute audit rounds
SERVING_READY_TTL_S = 900.0  # gateway stops routing when the last audit round is older than this
# Hardware attestation (gittensor/validator/serving/attest.py, docker/attest). Each round a random
# SERVING_ATTEST_COHORT_FRACTION of the READY miners (plus never-passed and last-round failures) is sent one fresh
# seed at the same instant; the runtime image's gt_attest fills the card's free VRAM and runs a deterministic GEMM
# chain. Expected digest and wall time come from the validator's own reference (same image). PASS needs the digest,
# wall <= SERVING_ATTEST_BUDGET_RATIO x reference, and >= SERVING_ATTEST_MIN_FILL_RATIO of the free VRAM filled
# (free = total - SERVING_VRAM_MODEL_RESERVED_BYTES; the release may override). Two hotkeys on one card cannot both
# fill it at once (P(caught) = fraction^2 per round). Never a strike: capacity 0 that round, re-challenged next.
SERVING_ATTEST_COHORT_FRACTION = 0.5
SERVING_ATTEST_ITERS = 3
SERVING_ATTEST_BUDGET_RATIO = 1.6
SERVING_ATTEST_MIN_FILL_RATIO = 0.6
SERVING_ATTEST_TIMEOUT = 45.0  # seconds: fill + chain on a 5090 is ~1.5 s; queued challenges show up as slow
SERVING_ATTEST_UUID_MEMORY_ROUNDS = 12  # a verdict (and a UUID claim) older than this pays nothing until renewed
# Every field of a card's report except the digest is the miner's own number, so the count of cards is held to two
# things the validator measures itself: each device index answers its own seed (seed + index), which the reference
# recomputes, and the whole reply must arrive within one card's budget plus this network slack — gt_attest runs the
# cards in parallel, so N real cards take one card's wall and one card faking N takes N of them. At most
# SERVING_ATTEST_MAX_CARDS are judged (and priced) per hotkey.
SERVING_ATTEST_RTT_SLACK_MS = 2_000.0
SERVING_ATTEST_MAX_CARDS = 8
# A card counts only with the model resident: free VRAM before the fill must be at most total minus this fraction of
# the reservation (a bare 5090 shows ~32 GB free, one holding the model ~8 GB). Every passing card is a card-hour, so
# a multi-GPU hotkey earns per card it actually serves from — a second card with nothing loaded earns nothing.
SERVING_ATTEST_MODEL_RESIDENT_RATIO = 0.8
SERVING_VRAM_MODEL_RESERVED_BYTES = 24e9  # what sparkinfer holds with the model loaded (7498736 on a 5090: 23.7 GB)
SERVING_VERIFY_WORKERS = 8  # concurrent /v1/score calls to the reference per round
# Latency credit on the validator-observed time to first streamed token (network + queue + prefill). An honest
# 5090 reports TTFT ~26 ms on-box (2026-08-27 conformance) and a 64-token answer in ~165 ms (2026-08-22/24); add
# validator<->miner RTT and an honest miner anywhere on earth lands well under FULL. Credit is flat to FULL and
# falls linearly to 0 at ZERO, so a miner proxying to a GPU in another region or queueing requests loses credit
# in proportion. Generation length does not enter (mini-soak 2026-08-27: total latency of 64-512-token answers
# scored a perfect miner at 0.24). Same-region proxying is NOT visible here; that is the one-GPU-many-hotkeys
# case, caught by the concurrent capacity probe.
SERVING_LATENCY_FULL_CREDIT_MS = 500.0
SERVING_LATENCY_ZERO_CREDIT_MS = 1_500.0
# Decode speed on served traffic. Each served request with at least SERVING_DECODE_MIN_TOKENS completion tokens
# yields a validator-observed decode rate, tokens / (total - TTFT); it is compared with what one honest card does on
# this runtime at the number of requests THIS validator had in flight to the miner (the release's blessing-time
# `speed.decode_per_request` curve, interpolated), so a miner busy with our own load is not penalised for it.
# Credit = min(1, observed / expected); under SERVING_DECODE_FLOOR_RATIO it is 0 — a card shared between hotkeys or a
# runtime that is not the blessed one is slower by integer factors under load, honest variance is +-10%. Round credit
# is the mean over the round's served requests (misses 0), so availability, latency and speed price together.
SERVING_DECODE_FLOOR_RATIO = 0.5
SERVING_DECODE_TOLERANCE_RATIO = 0.8  # full credit down to this fraction of expected: the curve is measured on-box, the
# validator observes stream delivery over the WAN (soak 5: honest cards read 0.75-0.96x); credit = min(1, ratio / this)
SERVING_DECODE_MIN_TOKENS = 32
SERVING_DECODE_PER_REQUEST_FALLBACK = ((1, 440.0), (6, 46.0), (16, 19.0))  # (concurrent requests, tok/s each)
# Per-audit verdict. The blessed runtime is bit-reproducible (sparkinfer 7498736, SPARKINFER_DETERMINISTIC=1), so an
# honest miner reproduces the reference's greedy tokens exactly and its logprobs to float noise; every planted
# cheater differs on every prompt (measured 2026-08-24, internal serving-experiments notes: honest max |delta| 0.0000,
# cheapest cheater min mean |delta| 0.0057 / min max |delta| 0.129). Both bands must hold for an audit to pass.
SERVING_AUDIT_MIN_PREFIX_AGREEMENT = 1.0  # fraction of reference greedy tokens reproduced before first divergence
SERVING_AUDIT_MAX_MEAN_ABS_LOGPROB_DIFF = 0.005  # mean |logprob delta| over the agreed prefix
SERVING_AUDIT_MAX_ABS_LOGPROB_DIFF = 0.10  # largest single-position |logprob delta|
# Every request served through the gateway is the audit: the validator teacher-forces the miner's completion under
# its reference and checks the tokens/logprobs (no synthetic audit prompts, nothing for a miner to fingerprint).
# Rolling window: the mean of per-request outcomes (1 pass / 0 fail; misses, timeouts and wrong model are 0) over
# the last SERVING_AUDIT_WINDOW verified requests of a (hotkey, release) must reach the threshold for the number
# seen so far (rows are (k, threshold), linearly interpolated, flat beyond the last row). With a deterministic
# runtime the honest mean is 1.0, so the bar only leaves room for transient misses. A *wrong answer* (tokens or
# logprobs outside the bands with aligned lengths) is not a miss: it wipes the window and quarantines the hotkey for
# SERVING_QUARANTINE_S — there is no honest way to produce one on the blessed pin.
# History: the 1b8b962 pin needed a positional-overlap window with a calibrated ramp (0.016 at k=1 .. 0.415 at
# k=20; 2026-08-22 notes). Recalibrate with
#   python scripts/serving_cheat_experiment.py analyze --honest <honest rows> --cheaters <cheater rows>
SERVING_AUDIT_WINDOW = 10
SERVING_AUDIT_WINDOW_THRESHOLDS = ((1, 0.8),)
# Verification is one reference prefill per request, so it is sampled: per (hotkey, round) every baseline prompt and
# every failed request is judged, and of the completed gateway requests a random max(SAMPLE_MIN, SAMPLE_FRACTION x n)
# — the floor fills a window in one round, the fraction bounds reference load under real traffic. Nothing on the
# wire says which requests were drawn.
SERVING_AUDIT_SAMPLE_FRACTION = 0.2
SERVING_AUDIT_SAMPLE_MIN = 10
SERVING_QUARANTINE_S = 3600.0
# Baseline traffic: every round the validator sends each serving axon this many baseline prompts of its own, at
# random moments spread over the round (so they do not mark the round boundary), over the same path as user
# traffic. Real traffic does not displace them — 2 x ~256 tokens per miner per round is noise for a card and well
# inside the permitted-validator budget — it just adds to the evidence. This is what lets a validator with no users
# verify miners at all, and keeps quiet hours from being a free period.
SERVING_BASELINE_PER_ROUND = 2
# Most UIDs are not compute miners: OSS miners run no neuron, and validators' axons exist for PAT traffic. axon.is_serving
# alone still nominates them, and every baseline prompt to one costs a request_timeout. A hotkey that answers no
# request with a completion for SERVING_DORMANT_AFTER_ROUNDS consecutive rounds (timeouts, refusals, or an axon that
# says "Success" and serves nothing) is *dormant*: it leaves the round report, is not persisted, and receives no
# baseline prompts — except one retry every SERVING_DORMANT_RETRY_ROUNDS so a miner that comes online later is picked
# up within the hour. A completion resets it. A serving-miner registry replaces this heuristic later.
SERVING_DORMANT_AFTER_ROUNDS = 3
SERVING_DORMANT_RETRY_ROUNDS = 12
SERVING_API_DEFAULT_PORT = 8790
# Miner: a hotkey may query for inference only with at least this much stake on the subnet (alpha, metagraph.S). Set so
# that only the reference-running validator clears it (2026-08-26: one hotkey holds >1M alpha, the next 0.27M); every
# other caller goes through that validator's gateway. Env SERVING_MIN_CALLER_STAKE.
SERVING_MIN_CALLER_STAKE = 1_000_000.0
# Any hotkey holding a validator permit may also query, any request shape, up to this many completion tokens per
# tempo (metagraph.block // BLOCKS_PER_TEMPO): enough to run an independent reference and audit a fleet of ~100
# miners several times over, worthless as free inference (~$0.05 of tokens). No shape limit, so a smaller
# validator's audits look like any other request.
SERVING_VALIDATOR_TOKENS_PER_TEMPO = 50_000
# Validator: a miner's "budget spent" refusal is judged neutral (not a miss) only when this validator's own ledger
# of max_tokens sent to that miner in the trailing tempo has reached this fraction of the allowance. The refusal
# text is the miner's to write; the ledger is not. A staked caller has no budget and is never refused on one.
SERVING_BUDGET_REFUSAL_RATIO = 0.8
BLOCKS_PER_TEMPO = 360
SERVING_BACKEND_CONCURRENCY = 16  # miner: concurrent backend generations (sparkinfer >= 12954e6 handles 24)
SERVING_SEEN_NONCES = 10_000  # miner: replay guard size; covers many minutes of validator traffic
SERVING_MAX_TOKENS = 1024  # hard cap per request (API and miner both enforce)
# Gateway: total characters across a request's messages (~4 per token). A prompt past the release's context makes
# the runtime answer 400 — not the miner's fault, but every such request used to land in its window as a miss, so
# one key holder could take any miner off READY with a few oversized prompts. The cap is set under the blessed
# release's context; a request the runtime still rejects is checked against the reference before it counts.
SERVING_MAX_PROMPT_CHARS = 16_000
SERVING_DB_RETENTION_DAYS = 7  # validator: per-round serving rows older than this are pruned on each write
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
