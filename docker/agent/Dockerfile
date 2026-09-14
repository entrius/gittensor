# syntax=docker/dockerfile:1.7
# Gittensor compute agent: the one privileged container a miner runs with `gitt up` (vault 23 §2, 24 §3 WS-A).
# sshd (root, keys only) + the agent app (gittensor/agent: POST/DELETE /install_ssh_key signed by the controller
# hotkey, GET /info). The controller does everything else over SSH against the host docker daemon.
#
#   docker build -f docker/agent/Dockerfile -t entrius/gt-agent:dev .
#
# How it is run (exactly; gittensor/agent/launch.py::agent_run_command and docker/agent/runner.sh agree on this):
#
#   docker run -d --name gt-agent --restart unless-stopped --privileged --pid host --gpus all \
#     -v /var/run/docker.sock:/var/run/docker.sock -v gt-agent-ssh:/var/lib/gt-agent \
#     -p 2200:2200 -p 8200:8200 \
#     -e GT_AGENT_SSH_PORT=2200 -e GT_AGENT_HTTP_PORT=8200 -e GT_AGENT_MINER_HOTKEY=<ss58> \
#     -e GT_AGENT_IMAGE=entrius/gt-agent:stable -e GT_AGENT_IMAGE_DIGEST=<repo digest> \
#     -e NVIDIA_DRIVER_CAPABILITIES=all entrius/gt-agent:stable
#
# No CUDA base: the NVIDIA container toolkit injects nvidia-smi and libnvidia-ml into any `--gpus` container.
# Debian trixie so sshd is OpenSSH >= 9.8 and `PerSourcePenalties no` is a known directive (see sshd.conf).
FROM python:3.12-slim-trixie

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/gt-agent

# docker-ce-cli: docker-py's ssh:// transport runs `docker system dial-stdio` on the box, and the controller's
# shell sessions drive the host daemon with the plain CLI.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/ssh/ssh_host_* \
    && mkdir -p /run/sshd /root/.ssh /var/lib/gt-agent \
    && chmod 700 /root/.ssh

# sr25519 verification only; the full `bittensor` package is not needed in the agent.
RUN pip install --no-cache-dir bittensor-wallet==4.0.1

COPY docker/agent/sshd.conf /etc/ssh/sshd_config.d/gt-agent.conf
# Fail the build, not the container start, on a directive this sshd refuses (a throwaway host key: the image
# ships none; the entrypoint keeps the real one in the gt-agent-ssh volume).
RUN ssh-keygen -q -t ed25519 -N '' -f /tmp/hostkey \
    && /usr/sbin/sshd -T -h /tmp/hostkey >/dev/null \
    && /usr/sbin/sshd -T -h /tmp/hostkey | grep -qxE 'permitrootlogin (without-password|prohibit-password)' \
    && /usr/sbin/sshd -T -h /tmp/hostkey | grep -qx 'passwordauthentication no' \
    && rm -f /tmp/hostkey /tmp/hostkey.pub

COPY gittensor/__init__.py /opt/gt-agent/gittensor/__init__.py
COPY gittensor/agent/ /opt/gt-agent/gittensor/agent/
COPY neurons/agent.py /opt/gt-agent/neurons/agent.py
COPY docker/agent/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

ARG GT_AGENT_VERSION=dev
ENV GT_AGENT_SSH_PORT=2200 \
    GT_AGENT_HTTP_PORT=8200 \
    NVIDIA_DRIVER_CAPABILITIES=all
EXPOSE 2200 8200
LABEL io.gittensor.agent="gt-agent" \
      io.gittensor.agent_version="${GT_AGENT_VERSION}"
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:${GT_AGENT_HTTP_PORT}/info" || exit 1
ENTRYPOINT ["/entrypoint.sh"]
