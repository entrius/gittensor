# syntax=docker/dockerfile:1.7
# Gittensor-built image of the blessed serving runtime: sparkinfer_server at a pinned upstream commit.
#
# We build this ourselves (rather than depending on upstream publishing images) so that the image
# digest IS the release: `runtime_pin` in the serving loadout names the SPARKINFER_REF this was built
# from, validators run it as their live reference, miners run it to serve. Blessing a new upstream
# optimisation = bump SPARKINFER_REF, let CI build + push, run scripts/check_serving_runtime.py on the
# reference, update the loadout. See docs/serving-runtime-contract.md.
#
#   docker build -f docker/sparkinfer.Dockerfile --build-arg SPARKINFER_REF=<commit> -t entrius/sparkinfer:<commit> .
#   docker run --gpus all -p 8080:8080 -v $PWD/data/models:/opt/sparkinfer/models entrius/sparkinfer:<commit>

ARG CUDA_VERSION=12.8.1
ARG UBUNTU_VERSION=24.04

# ---------- build ----------
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu${UBUNTU_VERSION} AS build
ARG SPARKINFER_REPO=https://github.com/gittensor-ai-lab/sparkinfer
ARG SPARKINFER_REF=main
# sm_120 = RTX 5090. Add more (e.g. "89;120") only if the fleet blesses other hardware.
ARG CUDA_ARCHS=120

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl build-essential cmake ninja-build pkg-config python3 \
    && rm -rf /var/lib/apt/lists/*
# Rust is build-time only: tokenizers-cpp wraps HF `tokenizers` (server/README.md).
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH=/root/.cargo/bin:$PATH

WORKDIR /src
RUN git clone ${SPARKINFER_REPO} sparkinfer \
    && git -C sparkinfer checkout ${SPARKINFER_REF} \
    && git -C sparkinfer rev-parse HEAD > /src/SPARKINFER_COMMIT
# Build everything, not just the server target: sparkinfer_server links the shared
# libsparkinfer_runtime.so, and run.sh's ensure_sparkinfer() refuses to start unless the
# runtime bench binaries (qwen3_gguf_bench/score/prefill_check) exist under build/runtime.
RUN cmake -S sparkinfer -B sparkinfer/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DBUILD_SERVER=ON -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCHS} \
    && cmake --build sparkinfer/build
# Hardware attestation challenge (docker/attest): deterministic fp32 GEMM chain + VRAM fill. -fmad=false so the
# summation is bit-identical on every card of the architecture (the validator's reference recomputes the digest).
COPY docker/attest/gt_attest.cu /src/attest/gt_attest.cu
RUN nvcc -O3 -fmad=false -gencode arch=compute_${CUDA_ARCHS},code=sm_${CUDA_ARCHS} \
        -o /src/attest/gt_attest /src/attest/gt_attest.cu -lnvidia-ml

# ---------- runtime ----------
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu${UBUNTU_VERSION}
ENV DEBIAN_FRONTEND=noninteractive
# python3 + huggingface_hub for the model/tokenizer download in server/run.sh (curl is its fallback).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl python3 python3-pip bash \
    && pip3 install --no-cache-dir --break-system-packages "huggingface_hub[cli]" tokenizers \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/sparkinfer
COPY --from=build /src/sparkinfer/server/run.sh      server/run.sh
COPY --from=build /src/sparkinfer/bench/scripts       bench/scripts
COPY --from=build /src/sparkinfer/build/runtime       build/runtime
COPY --from=build /src/sparkinfer/build/moe           build/moe
COPY --from=build /src/sparkinfer/build/server        build/server
COPY --from=build /src/SPARKINFER_COMMIT              SPARKINFER_COMMIT
COPY --from=build /src/attest/gt_attest               bin/gt_attest
COPY docker/attest/attest_server.py                   bin/attest_server.py
COPY docker/attest/entrypoint.sh                      bin/entrypoint.sh
# sparkinfer_server links libsparkinfer_runtime.so + libsparkinfer_moe.so (ldd-verified on 1b8b962, 9e43bfa).
ENV LD_LIBRARY_PATH=/opt/sparkinfer/build/runtime:/opt/sparkinfer/build/moe:${LD_LIBRARY_PATH}

# Bake the pin into the image so a running container can report what it is (contract P2).
# MODEL_SHA256: the blessed model file digest. Upstream's bench/scripts/reference.lock pins one too, but
# HF repos get re-uploaded (unsloth's Qwen3.6 GGUF changed under sparkinfer 1b8b962's lock, 2026-08-21) and
# run.sh then re-downloads forever. The release in serving_loadout.json carries model_sha256; pass it as
# -e MODEL_SHA256=... (compose does) so the container verifies against OUR pin.
# SPARKINFER_DETERMINISTIC=1: bit-reproducible greedy decode (contract D1). Upstream ships it opt-in
# (sparkinfer#910, 9e43bfa); the blessed release REQUIRES it — the validator's reference and every
# miner must produce identical logprobs for identical requests. Decode speed unchanged, TTFT +2-8%.
ARG SPARKINFER_REF=main
ENV SPARKINFER_REF=${SPARKINFER_REF} \
    SPARKINFER_DETERMINISTIC=1 \
    MODELS_DIR=/opt/sparkinfer/models \
    HOST=0.0.0.0 \
    PORT=8080
VOLUME ["/opt/sparkinfer/models"]
# 8080 inference, 8081 attestation sidecar (docker/attest/attest_server.py)
EXPOSE 8080 8081
LABEL org.opencontainers.image.source="https://github.com/gittensor-ai-lab/sparkinfer" \
      io.gittensor.serving.runtime="sparkinfer" \
      io.gittensor.serving.runtime_ref="${SPARKINFER_REF}"

HEALTHCHECK --interval=30s --timeout=5s --start-period=600s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8080/v1/models || exit 1

# bin/entrypoint.sh starts the attestation sidecar (:8081), then run.sh downloads the blessed model + tokenizer into
# MODELS_DIR on first start (--download) and execs the prebuilt server binary.
ENTRYPOINT ["bash", "bin/entrypoint.sh"]
