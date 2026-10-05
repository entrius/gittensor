# syntax=docker/dockerfile:1.7
# Gittensor compute agent: the one privileged container a miner runs with `gitt up` (vault 23 §2, 24 §3 WS-A, 26 §5).
# It is an sshd and nothing else. sshd trusts ONE SSH certificate authority (our controller's) compiled into the
# image; the controller mints a ~5-minute certificate per visit and does everything over SSH against the host
# docker daemon. No agent HTTP port, no key-install route, no Python.
#
#   docker build -f docker/agent/Dockerfile --build-arg GT_CA_PUB="$(cat docker/agent/keys/gt_ca.pub)" \
#       -t entrius/gt-agent:dev .
#
# GT_CA_PUB is the CA's PUBLIC key (one `ssh-ed25519 AAAA...` line). The build fails without it. A key from
# docker/agent/keys/make-dev-keys.sh carries `DO-NOT-SHIP` in its comment and the entrypoint refuses to start on it
# unless GT_AGENT_ALLOW_DEV_KEYS=1 (what `gitt up --no-update --allow-dev-keys` sets for local builds).
#
# How it is run (exactly; gittensor/agent/launch.py::agent_run_command and docker/agent/runner.sh agree on this):
#
#   docker run -d --name gt-agent --restart unless-stopped --privileged --pid host --gpus all \
#     -v /var/run/docker.sock:/var/run/docker.sock -v gt-agent-ssh:/var/lib/gt-agent \
#     -p 2200:2200 -e GT_AGENT_SSH_PORT=2200 -e GT_AGENT_MINER_HOTKEY=<ss58> \
#     -e GT_AGENT_IMAGE=entrius/gt-agent@sha256:<digest> -e GT_AGENT_IMAGE_DIGEST=sha256:<digest> \
#     -e NVIDIA_DRIVER_CAPABILITIES=all entrius/gt-agent@sha256:<digest>
#
# No CUDA base: the NVIDIA container toolkit injects nvidia-smi and libnvidia-ml into any `--gpus` container.
# Debian trixie so sshd is OpenSSH >= 9.8 and `PerSourcePenalties no` is a known directive (see sshd.conf).
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

# docker-ce-cli: the controller's SSH sessions drive the host daemon with the plain CLI (and docker-py's ssh://
# transport runs `docker system dial-stdio` on the box). curl: the full check's network probe.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/ssh/ssh_host_* \
    && mkdir -p /run/sshd /var/lib/gt-agent

# The trust anchor. Baked in, never mounted, never an env var (vault 26 §5: rotating it ships an agent release).
ARG GT_CA_PUB
RUN test -n "$GT_CA_PUB" || { echo 'GT_CA_PUB build-arg (the controller CA public key) is required' >&2; exit 1; } \
    && printf '%s\n' "$GT_CA_PUB" > /etc/ssh/gt_ca.pub \
    && chmod 0644 /etc/ssh/gt_ca.pub \
    && ssh-keygen -lf /etc/ssh/gt_ca.pub >/dev/null

COPY docker/agent/sshd.conf /etc/ssh/sshd_config.d/gt-agent.conf
# Fail the build, not the container start, on a directive this sshd refuses (a throwaway host key: the image
# ships none; the entrypoint keeps the real one in the gt-agent-ssh volume).
RUN ssh-keygen -q -t ed25519 -N '' -f /tmp/hostkey \
    && /usr/sbin/sshd -T -h /tmp/hostkey >/dev/null \
    && /usr/sbin/sshd -T -h /tmp/hostkey | grep -qxE 'permitrootlogin (without-password|prohibit-password)' \
    && /usr/sbin/sshd -T -h /tmp/hostkey | grep -qx 'passwordauthentication no' \
    && /usr/sbin/sshd -T -h /tmp/hostkey | grep -qx 'trustedusercakeys /etc/ssh/gt_ca.pub' \
    && /usr/sbin/sshd -T -h /tmp/hostkey | grep -qx 'authorizedkeysfile none' \
    && rm -f /tmp/hostkey /tmp/hostkey.pub

COPY docker/agent/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

ARG GT_AGENT_VERSION=dev
ENV GT_AGENT_SSH_PORT=2200 \
    NVIDIA_DRIVER_CAPABILITIES=all
EXPOSE 2200
LABEL io.gittensor.agent="gt-agent" \
      io.gittensor.agent_version="${GT_AGENT_VERSION}"
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=5 \
    CMD sh -c 'ssh-keyscan -T 3 -p "$GT_AGENT_SSH_PORT" 127.0.0.1 2>/dev/null | grep -q ssh-ed25519'
ENTRYPOINT ["/entrypoint.sh"]
