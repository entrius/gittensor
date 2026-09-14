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
# The proof image (docker/proof/Dockerfile): our own small signed base, `entrius/gt-proof`, with NO binary and NO
# secret inside; the sealed binary is copied in over SSH at check time. Unpublished for now: build locally and run
# by tag, then pin the pushed digest here. With a digest set the controller runs `repo@sha256:...`.
PROOF_IMAGE_REPO = 'entrius/gt-proof'
PROOF_IMAGE_TAG = 'dev'
PROOF_IMAGE_DIGEST = ''

# Identity and resources.
AGENT_CONTAINER_NAME = 'gt-agent'  # the container `gitt up` starts (gittensor/agent/config.py AGENT_CONTAINER_NAME)
DISK_MIN_FREE_GB = 100.0  # weights + images; Lium's VerifyX floor is the same 100 GB
# A HOST path; '' = wherever the host docker daemon keeps images (`docker info` DockerRootDir). The scrape reads it
# through /proc/1/root: the agent runs with --pid host, and its own filesystem has no /var/lib/docker at all (first
# real box, 9/14: `df /var/lib/docker` inside gt-agent -> No such file or directory).
DISK_PATH = ''
HOST_ROOT = '/proc/1/root'
# Docker Hub is the registry (vault 23 §8); the weights come from Hugging Face at pre-staging.
NETWORK_TARGETS = ('https://registry-1.docker.io/v2/', 'https://huggingface.co/api/models/Qwen')
NETWORK_TIMEOUT_S = 10.0
NVIDIA_SMI_TIMEOUT_S = 15.0
SSH_COMMAND_TIMEOUT_S = 30.0
# The served NVML allowlist: JSON {driver_version: [md5, ...]}; a file path or an http(s) URL. Empty = nothing is
# allowlisted and every box fails the nvml_digest check (fail closed).
NVML_ALLOWLIST_LOCATION = ''

# State machine. Every idle card is probed at the same instant every 20 min (Kimbo 9/14), and the proof binary is
# rebuilt every round, so a forger has to crack that round's build inside the 30 s answer window; worst case is
# one cycle of idle pay before the bench. A BENCHED box waits out the ladder before it may re-enter through ADMIT,
# and the ladder resets after a long clean stretch.
FULL_CHECK_INTERVAL_S = 1200.0
BENCH_BACKOFF_LADDER_S = (3_600, 14_400, 57_600, 230_400)  # 1 h -> 4 h -> 16 h -> 64 h (vault `23` §5)
BENCH_LADDER_RESET_AFTER_S = 7 * 86_400
