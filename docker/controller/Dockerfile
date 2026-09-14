# syntax=docker/dockerfile:1.7
# The compute-pool controller (vault 26 §1, §3, §5, §8): `gitt controller` in its own container. It only makes
# outbound SSH connections — no docker socket, no published ports, no chain key. Everything it holds comes in as
# mounts, never baked in: the SSH CA private key, the state directory (boxes.json, known_hosts,
# nvml_allowlist.json), and the privately installed GPU-proof provider with its binary and secret store.
#
#   docker build -f docker/controller/Dockerfile -t entrius/gt-controller:dev .
#
# Run line and mounts: docker/controller/README.md. The per-round proof build is NOT in this image; `round --loop
# --build-cmd` is the seam for it.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# openssh-client: ssh, ssh-keygen (certificates) and ssh-keyscan (admit). Nothing else from the OS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/gittensor
COPY pyproject.toml README.md LICENSE ./
COPY gittensor ./gittensor
RUN pip install .

# Public packages a mounted provider needs (e.g. --build-arg EXTRA_PIP_PACKAGES="pynacl==1.6.0"). The provider itself
# is mounted and put on PYTHONPATH at run time, never installed here.
ARG EXTRA_PIP_PACKAGES=""
RUN if [ -n "$EXTRA_PIP_PACKAGES" ]; then pip install $EXTRA_PIP_PACKAGES; fi

# Runs as whatever --user the operator passes (the owner of the mounted state dir); HOME only has to be writable.
ENV HOME=/tmp
WORKDIR /state
ENTRYPOINT ["gitt", "controller"]
