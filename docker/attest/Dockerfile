# syntax=docker/dockerfile:1.7
# Gittensor attestation sidecar: one long-running container per miner box (and beside a validator's reference), on
# every GPU the box has. Answers the validator's hardware challenge (gt_attest: seeded VRAM fill + deterministic
# GEMM chain, docker/attest/gt_attest.cu) and a self-report (GET /info). Runtime-agnostic — the same image serves
# every blessed release, so blessing a new runtime never touches it. Pinned by `attest.image` in the serving loadout.
#
#   docker build -f docker/attest/Dockerfile -t entrius/gt-attest:<tag> .
#   docker run -d --name gt-attest --gpus all -p 8081:8081 entrius/gt-attest:<tag>

ARG CUDA_VERSION=12.8.1
ARG UBUNTU_VERSION=24.04

FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu${UBUNTU_VERSION} AS build
# sm_120 = RTX 5090. The digest is only comparable across cards of one architecture; add more only if the fleet
# blesses other hardware.
ARG CUDA_ARCHS=120
COPY docker/attest/gt_attest.cu /src/gt_attest.cu
# -fmad=false: the summation is bit-identical on every card of the architecture (the validator's reference recomputes
# the digest).
RUN nvcc -O3 -fmad=false -gencode arch=compute_${CUDA_ARCHS},code=sm_${CUDA_ARCHS} -Xcompiler -pthread \
        -o /src/gt_attest /src/gt_attest.cu -lnvidia-ml

FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu${UBUNTU_VERSION}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends python3 curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/gt-attest
COPY --from=build /src/gt_attest          bin/gt_attest
COPY docker/attest/attest_server.py       bin/attest_server.py
ARG GT_ATTEST_VERSION=dev
ENV GT_ATTEST_BIN=/opt/gt-attest/bin/gt_attest \
    GT_ATTEST_VERSION=${GT_ATTEST_VERSION} \
    ATTEST_PORT=8081
EXPOSE 8081
LABEL io.gittensor.serving.attest="gt-attest" \
      io.gittensor.serving.attest_version="${GT_ATTEST_VERSION}"
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8081/info || exit 1
ENTRYPOINT ["python3", "bin/attest_server.py"]
