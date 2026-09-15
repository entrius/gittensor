# syntax=docker/dockerfile:1.7
# The compute-pool gateway (vault 26 §1, §2): `gitt gateway` in its own container beside the controller. It serves one
# port to das, reads the controller's state directory from a read-only mount (instances.json, registry/,
# models_override.json) and talks plain HTTP to instances. No docker socket, no chain key, no Postgres.
#
#   docker build -f docker/gateway/Dockerfile -t entrius/gt-gateway:dev .
#
# Run line and das wiring: docker/gateway/README.md.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# openssh-client for ssh-keygen: every table read re-verifies the registry signatures with `ssh-keygen -Y verify`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/gittensor
COPY pyproject.toml README.md LICENSE ./
COPY gittensor ./gittensor
RUN pip install .

# A real user (ssh-keygen refuses a uid with no passwd entry). It only needs read access to the mounted state dir.
ARG GATEWAY_UID=1000
ARG GATEWAY_GID=1000
RUN groupadd -g "$GATEWAY_GID" gateway \
    && useradd -u "$GATEWAY_UID" -g "$GATEWAY_GID" -d /home/gateway -m -s /usr/sbin/nologin gateway
USER gateway
ENV HOME=/home/gateway
EXPOSE 8790
ENTRYPOINT ["gitt", "gateway"]
CMD ["--state-dir", "/state", "--listen", "0.0.0.0:8790"]
