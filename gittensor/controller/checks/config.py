# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tunables for the full hardware check and the GPU-proof slot (``24`` §3 WS-C).

Kept out of ``gittensor/constants.py`` on purpose: the controller is built on a branch against our own cards and
cut over in one flip, and nothing here is read by the live phase-0 path. Values copied from the phase-0 attest path
are marked as copies so the cutover can retire the originals without touching this file.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CardSpec:
    """What every card on an admitted box must look like in ``nvidia-smi --query-gpu``."""

    name: str = 'NVIDIA GeForce RTX 5090'
    compute_cap: str = '12.0'  # sm_120, what the proof kernel (docker/proof/kernel) is compiled for
    vram_total_mib_min: int = 32_000  # a 5090 reports 32607 MiB
    vram_total_mib_max: int = 33_000
    count_min: int = 1
    count_max: int = 8


RTX_5090 = CardSpec()

# Lium's floor (`checks/gpu_power_limit.py` MIN_POWER_LIMIT_RATIO): a card capped below 90% of its default limit is
# throttled on purpose.
POWER_LIMIT_MIN_RATIO = 0.9

# GPU proof (vault 23 §3a-b). The proof is a pluggable provider (gittensor/controller/proof); what the controller
# fixes here is the image its job runs in, the wall-clock budget for one card's job, and the fill the provider must
# demand: the FILL comes from OUR 5090 spec table (CardSpec.vram_total_mib_min), never from the box's self-report,
# so a 24 GB card cannot answer a 5090's challenge. Timing bands are the provider's own.
PROOF_FILL_RATIO = 0.9  # what the binary fills: 0.9 of the total CUDA reports (~30.3 GB on a 5090, ~3 GB left, like Lium's total − 2 GB)
# What the verdict demands, against OUR spec table: 0.85 × 32,000 MiB ≈ 28.5 GB. Measured 9/14: CUDA reports ~500 MiB
# less total than nvidia-smi, so judging at 0.9 of spec left a 100 MB margin on an honest 5090; 0.85 leaves ~2 GB
# (Kimbo 9/14) and is still 4.5 GB above anything a 24 GB card can fill.
PROOF_FILL_FLOOR_RATIO = 0.85
# The SSH command timeout for one card's `docker start -a`. The proof's own verdict limit is the provider's flat
# 30 s on our stopwatch (trust-the-seal, no 5090 speed band; Kimbo 9/14); this is only the hard stop after which we
# give up waiting for an answer at all, kept above the verdict limit so a late answer is judged, not lost.
PROOF_JOB_TIMEOUT_S = 60.0
# Asking the box whether it has the proof image (and starting its pull when it has not): a local docker call.
PROOF_IMAGE_PROBE_TIMEOUT_S = 20.0
# The proof image (docker/proof/Dockerfile): our own small signed base, `entrius/gt-proof`, with NO binary and NO
# secret inside; the sealed binary is copied in over SSH at check time. With a digest set the controller runs
# `repo@sha256:...` (a miner box pulls it from Docker Hub); empty = a locally built `repo:tag` (dev boxes).
PROOF_IMAGE_REPO = 'entrius/gt-proof'
PROOF_IMAGE_TAG = 'dev'
PROOF_IMAGE_DIGEST = 'sha256:e6be400329973f954459e2f0e760a66b27a423777c5de0bf1f667bf497cbb9fb'  # published with agent release 5.1.0 (9/17)

# Identity and resources.
AGENT_CONTAINER_NAME = 'gt-agent'  # the container `gitt up` starts (gittensor/agent/config.py AGENT_CONTAINER_NAME)
DISK_MIN_FREE_GB = 100.0  # weights + images; Lium's VerifyX floor is the same 100 GB
# A HOST path; '' = wherever the host docker daemon keeps images (`docker info` DockerRootDir). The scrape reads it
# through /proc/1/root: the agent runs with --pid host, and its own filesystem has no /var/lib/docker at all (first
# real box, 9/14: `df /var/lib/docker` inside gt-agent -> No such file or directory).
DISK_PATH = ''
HOST_ROOT = '/proc/1/root'
# Docker Hub is the registry (vault 23 §8); the weights come from Hugging Face at pre-staging. Evidence only since
# 9/19: one Hugging Face miss benched a healthy serving box and took the fleet to zero for 4 h. A box that cannot
# pull fails its start, which has its own path (FAILED_STARTS_BENCH_AFTER).
NETWORK_TARGETS = ('https://registry-1.docker.io/v2/', 'https://huggingface.co/api/models/Qwen')
NETWORK_TIMEOUT_S = 10.0
NVIDIA_SMI_TIMEOUT_S = 15.0
SSH_COMMAND_TIMEOUT_S = 30.0

# State machine. Every idle card is probed at the same instant every 20 min (Kimbo 9/14), and the proof binary is
# rebuilt every round, so a forger has to crack that round's build inside the 30 s answer window; worst case is
# one cycle of idle pay before the bench. A BENCHED box waits out the ladder before it may re-enter through ADMIT,
# and it earns the ladder back down with clean time.
FULL_CHECK_INTERVAL_S = 1200.0
BENCH_BACKOFF_LADDER_S = (3_600, 14_400, 57_600, 230_400)  # 1 h -> 4 h -> 16 h -> 64 h (vault `23` §5)
# A box SSH cannot reach gets no verdict (and no idle pay for that round: idle pay needs a passing proof). After
# this many consecutive unreachable rounds it is BENCHED for a flat UNREACHABLE_BENCH_S, off the fraud ladder: a
# dead link is not a caught cheat, but a box must not dodge a bench by dropping SSH forever (Kimbo 9/14).
UNREACHABLE_BENCH_AFTER = 3
UNREACHABLE_BENCH_S = 12 * 3_600
# The ladder steps back down with clean time (Kimbo 9/19): time the box is admitted and answering, leased or idle.
# The clock stops on an unreachable round or a check that could not run, and a bench starts it over. The numbers are
# standing's (STANDING_STANDARD_AFTER_S, STANDING_TRUSTED_AFTER_S). A forged proof is caught within one round, so a
# cheat never collects a step while cheating.
BENCH_LADDER_STEP_DOWN_S = 6 * 3_600  # one rung down per this much clean time
BENCH_LADDER_CLEAN_SLATE_S = 48 * 3_600  # rung 0, however many were climbed
# A check that could not be carried out (the proof container would not start, staging failed) is not a failed check
# (Kimbo 9/19; mainnet 9/19: a first real fault on rung 4 cost 64 h). The cards it was for go to CHECKING (unpaid, not
# leasable), the box is tried again after COULD_NOT_RUN_RETRY_S, and this many in a row bench it on the ladder: a box
# must not dodge a proof by breaking its own container runtime. The count starts over after a bench.
COULD_NOT_RUN_BENCH_AFTER = 3
COULD_NOT_RUN_RETRY_S = 0.9 * FULL_CHECK_INTERVAL_S  # about one round; under it so the next round is not skipped
# How much of a failed command's output is kept. The reason sits at the end of a docker error (9/19: 300 characters
# from the front cut off what NVIDIA's hook said), so the tail is what is kept.
ERROR_CLIP = 2_000

# Placement (24 §3 WS-B). A failed start (health not by `placement.max_load_s`, canary failed, artifact mismatch) is
# slow, not caught: undeploy, CHECKING, no bench. This many in a row on one box benches it on the ladder (Kimbo 9/14).
FAILED_STARTS_BENCH_AFTER = 3
# Pre-staged artifacts live on the HOST under this root, one directory per manifest name and volume, bind-mounted
# into the instance read-only. Fetch and hash run in a throwaway container, never in the workload (no egress there).
MODELS_ROOT = '/var/lib/gt-models'
MANIFEST_MOUNT = '/manifest.yaml'  # the BLESSED manifest, bind-mounted read-only over the image's baked copy
ARTIFACT_IMAGE = 'python:3.12-slim'
HF_HUB_VERSION = '1.31.0'  # huggingface_hub in the fetch container; `hf download --revision`
IMAGE_PULL_TIMEOUT_S = 1800.0
ARTIFACT_FETCH_TIMEOUT_S = 3600.0
# `network.egress: []` runs the instance on this bridge: IP masquerade off (no route out; published ports still
# answer) and inter-container traffic off. `--network none` cannot be used: docker silently drops `-p` with it.
NOEGRESS_NETWORK = 'gt-noegress'
HEALTH_POLL_S = 2.0
HTTP_PROBE_TIMEOUT_S = 10.0
RECONCILE_INTERVAL_S = 30.0

# The in-lease watch (24 §3 WS-D, 23 §5). One SSH visit per box with a LEASED card asks: same card, our container
# running, card ours alone. Any failure benches the box and withholds pay from that instant. The manifest health probe
# runs on its own `health.interval_s` in the same visit. A visit that gets no answer is a miss on the instance, not a
# verdict on the box (Kimbo 9/16): the first miss makes the instance unroutable at once (`healthy: false`, the gateway
# stops sending it traffic; a passing heartbeat restores it), and HEARTBEAT_UNREACHABLE_AFTER misses in a row end the
# lease (no bench, nothing withheld: the card goes to CHECKING, the reconciler undeploys the instance when the box
# answers again, the one-box probe re-proves the card). The round's own unreachable count and its 12 h bench stay.
HEARTBEAT_INTERVAL_S = 60.0
HEARTBEAT_UNREACHABLE_AFTER = 3
WATCH_TICK_S = 5.0  # how often the watch looks for a heartbeat or health probe that is due
POWER_LIMIT_TOLERANCE_W = 1.0  # nvidia-smi rounds the limit; a real change is tens of watts
# `gitt controller run` (one process). A box mid-start or mid-drain holds its lock for minutes: the proof round waits
# this long for it (a heartbeat visit takes seconds), then skips the box until the next round.
ROUND_BOX_LOCK_WAIT_S = 30.0
# The round is scheduled on the wall clock and the schedule is checked every ROUND_WAKE_S, never slept through in one
# long monotonic wait: a controller that slept (the 9/15 run: 86 min) runs the round it missed as soon as it wakes and
# resumes the cadence from there. A round more than ROUND_CATCH_UP_FACTOR x the interval after the last is logged as a
# catch-up; exactly one runs. A `--build-cmd` that fails keeps the previous proof binary and is retried on every wake.
ROUND_WAKE_S = 5.0
ROUND_CATCH_UP_FACTOR = 1.5
# A card that reaches CHECKING (drain done, failed start, health replacement) is re-proved on its own box at the next
# watch tick instead of waiting for the 20-min round (Kimbo 9/15). A re-prove that got no verdict (box busy, SSH down)
# is tried again after this long, not every tick.
REPROVE_RETRY_S = 60.0
# Discovery (24 §3 WS-A): how often `gitt controller run --discover` reads the metagraph for new, moved and deregistered
# boxes. A new box waits at most this plus one proof round (20 min) for its first check; axons change rarely.
DISCOVER_INTERVAL_S = 300.0
SHUTDOWN_GRACE_S = 120.0  # SIGTERM: how long the loops get to finish the visit in flight
STANDING_EVENTS_KEEP = 200  # dated events per box for WS-E to fold; the oldest are folded into one, never lost

# Standing (24 §3 WS-E, 23 §5): probation -> standard -> trusted, a pure fold of the box's dated events. Clean lease
# time (a `clean_lease` event on every normal drain, summed over the box's cards) raises it; a hard failure (a failed
# heartbeat, a failed full check or proof, an operator release from a bench) resets it to probation; a soft one (a
# failed start or drain, a health replacement, an unreachable bench) drops it one level. First guesses.
STANDING_STANDARD_AFTER_S = 6 * 3_600.0  # N: clean lease-hours since the last reset
STANDING_TRUSTED_AFTER_S = 48 * 3_600.0  # M

# Rotation (23 §8). A lease gets its cap when it becomes LEASED: LEASE_CAP_S x its box's standing multiplier x a random
# factor in [1 - jitter, 1 + jitter], drawn by the controller and kept in instances.json (nothing a miner sees derives
# it). Past the cap the lease is replaced first and drained after, at most ROTATION_MAX_FRACTION of leased cards at once
# (never fewer than one), never below a deployment's replica count. Set the cap from the measured cycle D (~70 s, 9/15).
LEASE_CAP_S = 3_600.0
LEASE_CAP_JITTER = 0.2
LEASE_CAP_MULTIPLIER = {'probation': 0.5, 'standard': 1.0, 'trusted': 2.0}
ROTATION_MAX_FRACTION = 0.10
# A planned drain (rotation, in-place cycle, scale-down) waits for the gateway before it stops the container: until the
# gateway has re-read instances.json (it routes nothing new to a draining instance) and reports no request in flight
# on it. Bounded: the longest answer das allows is 330 s. With no gateway to ask (or one that does not answer), the
# short fixed grace covers the gateway's refresh and nothing more.
DRAIN_WAIT_MAX_S = 330.0
DRAIN_WAIT_POLL_S = 2.0
DRAIN_GRACE_S = 6.0

# The lease accounting check (usage_check.py): a leased card serves the gateway's traffic only. On every heartbeat
# visit the runtime's own counters are read beside the gateway's per-instance totals; the completion tokens the runtime
# made beyond everything the gateway sent it (plus an upper bound for each request whose count the gateway did not
# learn, and for each request in flight) is the surplus. Over max(MIN_TOKENS, MIN_FRACTION x the runtime's tokens) is
# a strike; EXTERNAL_USE_STRIKES in a row are a detection: the instance is drained to IDLE through the planned drain,
# an `external_use` SOFT standing event, and the box takes no new lease for EXTERNAL_USE_COOLDOWN_S. The
# EXTERNAL_USE_BENCH_AFTER-th detection inside EXTERNAL_USE_WINDOW_S is a bench that enters the ladder no lower than
# rung EXTERNAL_USE_BENCH_RUNG + 1 (16 h, then 64 h). Kimbo 9/18.
EXTERNAL_USE_MIN_TOKENS = 2_000
EXTERNAL_USE_MIN_FRACTION = 0.05
EXTERNAL_USE_STRIKES = 2
EXTERNAL_USE_COOLDOWN_S = 3_600.0
EXTERNAL_USE_BENCH_AFTER = 3
EXTERNAL_USE_WINDOW_S = 7 * 86_400.0
EXTERNAL_USE_BENCH_RUNG = 2  # bench_count is at least this before the bench climbs one rung: 16 h
EXTERNAL_USE_REASON = 'suspected external (non-gateway) usage'
# The most completion tokens one request may produce when it names no max_tokens: the runtime's own output limit
# (sparkinfer's SPARKINFER_MAX_OUTPUT_TOKENS default). A manifest that sets a higher limit raises it for its instances.
RUNTIME_OUTPUT_CEILING_TOKENS = 16_384
# Which of a runtime's /metrics series the check reads, keyed by the manifest's `runtime`: a metric name and the labels
# a series must carry (series that match are summed). A runtime not listed here is not checked.
RUNTIME_COUNTERS: dict[str, dict[str, tuple[str, dict[str, str]]]] = {
    'sparkinfer': {
        'completion_tokens': ('sparkinfer_tokens_total', {'kind': 'completion'}),
        'prompt_tokens': ('sparkinfer_tokens_total', {'kind': 'prompt'}),
        'requests': ('sparkinfer_requests_total', {}),
        'active': ('sparkinfer_active_requests', {}),
    },
}
# Recorded only, never acted on (Kimbo 9/18): the gateway's rolling median decode rate over requests that ran alone on
# their card, below this fraction of the manifest's profile.decode_tps_single once there are this many of them, is
# written to the operator log as evidence.
THROUGHPUT_EVIDENCE_FRACTION = 0.6
THROUGHPUT_EVIDENCE_MIN_N = 20
DECODE_TPS_WINDOW = 100  # the gateway's rolling window per instance

# Pay (24 §3 WS-F, 23 §7). The ledger settles every card every SETTLEMENT_TICK_S (one block) from the recorded state;
# the scorecard pays the trailing SETTLEMENT_WINDOW_S (phase 0's settlement window was one hour too).
SETTLEMENT_TICK_S = 12.0
SETTLEMENT_WINDOW_S = 3_600.0
# Idle pay needs a passing proof no older than this: one missed 20-min round is tolerated, a second stops idle pay.
IDLE_PROOF_MAX_AGE_S = 1.5 * FULL_CHECK_INTERVAL_S
# A hard failure forfeits the box's leased accrual over [its UTC day - 1 day, its UTC day + 1 day) (23 §5).
WITHHELD_DAYS_BEFORE = 1
WITHHELD_DAYS_AFTER = 1
# The alpha the compute pool pays with: the miners' part of the subnet's per-block alpha emission (41% of 1 alpha per
# block, dTAO before any halving) x the compute share of miner weights (1 - OSS_EMISSION_SHARE, read where it is used).
MINER_ALPHA_PER_BLOCK = 0.41
BLOCK_S = 12.0

# Price oracle (23 §7b "fail safe"). Hold the last good price on any failure; a read more than ORACLE_MAX_MOVE x away
# from it is refused until ORACLE_CONFIRM_READS reads in a row agree with each other; before any good read, the static
# prices. TAO is priced high there, like phase 0's fallback, so an unpriced pool undersizes its alpha, never overpays.
ORACLE_REFRESH_S = 600.0
ORACLE_MAX_MOVE = 2.0
ORACLE_CONFIRM_READS = 3
STATIC_TAO_USD = 400.0
STATIC_ALPHA_TAO = 0.003
METAGRAPHED_URL = ''  # metagraphed's REST base URL; '' = the static prices only
# The default price source (Kimbo 9/15: "whatever phase 0 did"): TAO/USD from CoinGecko's free endpoint, exactly as
# the retired serving pricing did, and alpha/TAO from the chain itself (the subnet pool's price, read-only).
COINGECKO_TAO_USD_URL = 'https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd'
PRICE_SOURCES = ('coingecko+chain', 'metagraphed', 'static')
PRICE_SOURCE = 'coingecko+chain'
NETUID = 74

# The signed scorecard (23 §8a, 26 §10 item 4). Written every SCORECARD_INTERVAL_S; valid_until = issued_at +
# SCORECARD_TTL_INTERVALS x the interval. A validator refuses it after that, and the compute share recycles.
SCORECARD_INTERVAL_S = 1_200.0
SCORECARD_TTL_INTERVALS = 2

# The public fleet document (publish.py): rewritten this often while the controller runs, and on every scorecard.
# A reader calls it stale after three of these.
PUBLISH_INTERVAL_S = 30.0
