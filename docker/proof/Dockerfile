# syntax=docker/dockerfile:1.7
# entrius/gt-proof: the small signed base image the GPU proof runs in (vault 23 §3a, §6; 24 §3 WS-C).
#
# It carries NO proof binary and NO secret. At check time the controller `docker create`s one of these per card
# (pinned with --gpus device=<uuid>), copies the current sealed binary and its challenge in over the SSH session
# (`docker cp -`), and `docker start -a`s every card's container at the same instant. proof_job.py runs whatever
# was staged at /opt/gt-proof/bin/gt_proof and prints its JSON plus an outer clock. The signed-image rule has no
# exception: the agent refuses any image not signed by us, so the proof job lives in this image and not in an
# ad-hoc one.
#
#   docker build -f docker/proof/Dockerfile -t entrius/gt-proof:dev .
#
# Pin the pushed digest in gittensor/controller/checks/config.py PROOF_IMAGE_DIGEST.
# The public kernel the sealed binary wraps is docker/proof/kernel/gt_gemm.cu; it is NOT built into this image.

ARG CUDA_VERSION=12.8.1
ARG UBUNTU_VERSION=24.04

FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu${UBUNTU_VERSION}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/gt-proof
RUN mkdir -p bin work && chmod 0755 bin work
COPY docker/proof/proof_job.py bin/proof_job.py
ARG GT_PROOF_VERSION=dev
ENV GT_PROOF_BIN=/opt/gt-proof/bin/gt_proof \
    GT_PROOF_VERSION=${GT_PROOF_VERSION}
LABEL io.gittensor.compute.proof="gt-proof" \
      io.gittensor.compute.proof_version="${GT_PROOF_VERSION}"
ENTRYPOINT ["python3", "bin/proof_job.py"]
