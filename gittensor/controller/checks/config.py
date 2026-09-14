# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tunables for the full hardware check and the challenge bank (``24`` §3 WS-C).

Kept out of ``gittensor/constants.py`` on purpose: the controller is built on a branch against our own cards and
cut over in one flip, and nothing here is read by the live phase-0 path. Values copied from the phase-0 attest path
are marked as copies so the cutover can retire the originals without touching this file.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CardSpec:
    """What every card on an admitted box must look like in ``nvidia-smi --query-gpu``."""

    name: str = 'NVIDIA GeForce RTX 5090'
    compute_cap: str = '12.0'  # sm_120, what the challenge kernel is compiled for
    vram_total_mib_min: int = 32_000  # a 5090 reports 32607 MiB
    vram_total_mib_max: int = 33_000
    count_min: int = 1
    count_max: int = 8


RTX_5090 = CardSpec()

# Lium's floor (`checks/gpu_power_limit.py` MIN_POWER_LIMIT_RATIO): a card capped below 90% of its default limit is
# throttled on purpose.
POWER_LIMIT_MIN_RATIO = 0.9

# GPU proof. BUDGET / MIN_FILL / ITERS are copies of SERVING_ATTEST_BUDGET_RATIO, SERVING_ATTEST_MIN_FILL_RATIO and
# SERVING_ATTEST_ITERS in gittensor/constants.py (values as of 2026-09-13), copied rather than imported so the
# cutover can delete them. The bank's wall time replaces the live reference's.
CHALLENGE_BUDGET_RATIO = 1.6
CHALLENGE_MIN_FILL_RATIO = 0.6
CHALLENGE_ITERS = 3
# Fraction of total VRAM the one-shot job fills: 0.9 of a 32 GB card is ~29.3 GiB (~31.5 GB), which only fits on an
# empty card. The digest does not depend on the fill; the wall time does, so the bank records the ratio it was made
# with and the consumer refuses an answer produced with other parameters.
CHALLENGE_FILL_RATIO = 0.9
# Our own clock around the whole SSH round trip + `docker run` (container start, CUDA init, fill, chain) is judged
# against the bank's outer clock (`run_ms`) x the ratio plus this slack; a copy of SERVING_ATTEST_RTT_SLACK_MS.
CHALLENGE_RTT_SLACK_MS = 2_000.0
CHALLENGE_DIM = 1024
CHALLENGE_MATRICES = 512
CHALLENGE_JOB_TIMEOUT_S = 180.0  # docker start + fill + chain; the chain alone is ~1.5 s on an idle 5090
# The challenge image (docker/challenge/Dockerfile). Unpublished for now: build it locally and run by tag, then pin
# the digest here once it is pushed. With a digest set the controller runs `image@sha256:...`.
CHALLENGE_IMAGE = 'ghcr.io/entrius/gt-challenge'
CHALLENGE_IMAGE_TAG = 'dev'
CHALLENGE_IMAGE_DIGEST = ''
BANK_LOW_WATER = 20  # unused seeds left before the consumer reports the bank as running low

# Identity and resources.
AGENT_CONTAINER_NAME = 'gittensor-agent'  # the container `gitt up` starts; must match WS-A's docker run --name
DISK_MIN_FREE_GB = 100.0  # weights + images; Lium's VerifyX floor is the same 100 GB
DISK_PATH = '/var/lib/docker'
NETWORK_TARGETS = ('https://ghcr.io/v2/', 'https://huggingface.co/api/models/Qwen')
NETWORK_TIMEOUT_S = 10.0
NVIDIA_SMI_TIMEOUT_S = 15.0
SSH_COMMAND_TIMEOUT_S = 30.0
# The served NVML allowlist: JSON {driver_version: [md5, ...]}; a file path or an http(s) URL. Empty = nothing is
# allowlisted and every box fails the nvml_digest check (fail closed).
NVML_ALLOWLIST_LOCATION = ''

# State machine. IDLE boxes get the full check every ~75 blocks; a BENCHED box waits out the ladder before it may
# re-enter through ADMIT, and the ladder resets after a long clean stretch.
FULL_CHECK_INTERVAL_S = 900.0
BENCH_BACKOFF_LADDER_S = (3_600, 14_400, 57_600, 230_400)  # 1 h -> 4 h -> 16 h -> 64 h (vault `23` §5)
BENCH_LADDER_RESET_AFTER_S = 7 * 86_400
