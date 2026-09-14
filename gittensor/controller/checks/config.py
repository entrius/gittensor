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
PROOF_FILL_RATIO = 0.9  # 0.9 of a 32 GB card is ~29 GiB: only an empty 5090 can give it
PROOF_JOB_TIMEOUT_S = 180.0  # docker start + CUDA init + fill + kernel; the kernel alone is ~1.5 s on an idle 5090
# The proof image (docker/proof/Dockerfile): our own small signed base, `entrius/gt-proof`, with NO binary and NO
# secret inside; the sealed binary is copied in over SSH at check time. Unpublished for now: build locally and run
# by tag, then pin the pushed digest here. With a digest set the controller runs `repo@sha256:...`.
PROOF_IMAGE_REPO = 'entrius/gt-proof'
PROOF_IMAGE_TAG = 'dev'
PROOF_IMAGE_DIGEST = ''

# Identity and resources.
AGENT_CONTAINER_NAME = 'gt-agent'  # the container `gitt up` starts (gittensor/agent/config.py AGENT_CONTAINER_NAME)
DISK_MIN_FREE_GB = 100.0  # weights + images; Lium's VerifyX floor is the same 100 GB
DISK_PATH = '/var/lib/docker'
# Docker Hub is the registry (vault 23 §8); the weights come from Hugging Face at pre-staging.
NETWORK_TARGETS = ('https://registry-1.docker.io/v2/', 'https://huggingface.co/api/models/Qwen')
NETWORK_TIMEOUT_S = 10.0
NVIDIA_SMI_TIMEOUT_S = 15.0
SSH_COMMAND_TIMEOUT_S = 30.0
# The served NVML allowlist: JSON {driver_version: [md5, ...]}; a file path or an http(s) URL. Empty = nothing is
# allowlisted and every box fails the nvml_digest check (fail closed).
NVML_ALLOWLIST_LOCATION = ''

# State machine. Every idle card is probed at the same instant every ~5 min (Kimbo 9/13; cadence TBD, the old 15
# was arbitrary); a BENCHED box waits out the ladder before it may re-enter through ADMIT, and the ladder resets
# after a long clean stretch.
FULL_CHECK_INTERVAL_S = 300.0
BENCH_BACKOFF_LADDER_S = (3_600, 14_400, 57_600, 230_400)  # 1 h -> 4 h -> 16 h -> 64 h (vault `23` §5)
BENCH_LADDER_RESET_AFTER_S = 7 * 86_400
