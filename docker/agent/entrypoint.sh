#!/bin/bash
# gt-agent entrypoint: sshd, root by certificate only, port from env. That is the whole agent.
set -euo pipefail

SSH_PORT="${GT_AGENT_SSH_PORT:-2200}"
STATE_DIR="${GT_AGENT_STATE_DIR:-/var/lib/gt-agent}"
CA_PUB="${GT_AGENT_CA_PUB:-/etc/ssh/gt_ca.pub}"
KEY_DIR="$STATE_DIR/ssh"
HOST_KEY="$KEY_DIR/ssh_host_ed25519_key"

# Refuse to run on a dev CA. make-dev-keys.sh tags its keys DO-NOT-SHIP; an image built on one must never sit on a
# box reachable from the internet, because anyone with the dev private key is root on it.
if [ ! -s "$CA_PUB" ]; then
    echo "gt-agent: no CA public key at $CA_PUB; this image was built wrong" >&2
    exit 1
fi
if grep -q 'DO-NOT-SHIP' "$CA_PUB" && [ "${GT_AGENT_ALLOW_DEV_KEYS:-0}" != 1 ]; then
    echo "gt-agent: the CA key baked into this image is a DEV key (DO-NOT-SHIP). Refusing to start." >&2
    echo "gt-agent: for a local build set GT_AGENT_ALLOW_DEV_KEYS=1 (gitt up --no-update --allow-dev-keys)." >&2
    exit 1
fi

mkdir -p /run/sshd "$KEY_DIR"

# The host key lives in the gt-agent-ssh volume so it survives self-updates: the controller pins it at ADMIT.
if [ ! -f "$HOST_KEY" ]; then
    ssh-keygen -q -t ed25519 -N '' -f "$HOST_KEY"
fi
chmod 600 "$HOST_KEY"
echo "gt-agent: sshd on :$SSH_PORT, host key $(ssh-keygen -lf "$HOST_KEY.pub" | cut -d' ' -f2), CA $(ssh-keygen -lf "$CA_PUB" | cut -d' ' -f2)"

exec /usr/sbin/sshd -D -e -p "$SSH_PORT" -h "$HOST_KEY"
