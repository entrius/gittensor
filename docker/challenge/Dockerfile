# syntax=docker/dockerfile:1.7
# Gittensor GPU proof, as a one-shot job (vault `23` §3, `24` §3 WS-C). The controller runs this image on ONE empty
# card of a miner box and reads one JSON line; there is no server and no sidecar. The kernel is a copy of the phase-0
# attest kernel (docker/attest/gt_attest.cu) with a fill ratio instead of fill-everything; the same image, on a card
# we own, generates the challenge bank (python -m gittensor.controller.challenge.bank generate).
#
#   docker build -f docker/challenge/Dockerfile -t ghcr.io/entrius/gt-challenge:dev .
#   docker run --rm --gpus '"device=GPU-<uuid>"' ghcr.io/entrius/gt-challenge:dev --seed 12345 --fill-ratio 0.9 --iters 3
#
# Pin the pushed digest in gittensor/controller/checks/config.py CHALLENGE_IMAGE_DIGEST.

ARG CUDA_VERSION=12.8.1
ARG UBUNTU_VERSION=24.04

FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu${UBUNTU_VERSION} AS build
# sm_120 = RTX 5090. The digest is only comparable across cards of one architecture; the bank is per architecture.
ARG CUDA_ARCHS=120
COPY docker/challenge/gt_challenge.cu /src/gt_challenge.cu
# -fmad=false: the summation is bit-identical on every card of the architecture, which is what makes a precomputed
# bank the expected answer.
RUN nvcc -O3 -fmad=false -gencode arch=compute_${CUDA_ARCHS},code=sm_${CUDA_ARCHS} \
        -o /src/gt_challenge /src/gt_challenge.cu -lnvidia-ml

FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu${UBUNTU_VERSION}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/gt-challenge
COPY --from=build /src/gt_challenge          bin/gt_challenge
COPY docker/challenge/challenge_job.py       bin/challenge_job.py
ARG GT_CHALLENGE_VERSION=dev
ENV GT_CHALLENGE_BIN=/opt/gt-challenge/bin/gt_challenge \
    GT_CHALLENGE_VERSION=${GT_CHALLENGE_VERSION}
LABEL io.gittensor.compute.challenge="gt-challenge" \
      io.gittensor.compute.challenge_version="${GT_CHALLENGE_VERSION}"
ENTRYPOINT ["python3", "bin/challenge_job.py"]
