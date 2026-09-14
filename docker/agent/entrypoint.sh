#!/bin/bash
# gt-agent entrypoint: sshd (root, keys only, port from env) + the agent app, one container. Either dying ends the
# container so docker's restart policy (and the runner) bring it back.
set -euo pipefail

SSH_PORT="${GT_AGENT_SSH_PORT:-2200}"
STATE_DIR="${GT_AGENT_STATE_DIR:-/var/lib/gt-agent}"
KEY_DIR="$STATE_DIR/ssh"
HOST_KEY="$KEY_DIR/ssh_host_ed25519_key"
AUTHORIZED_KEYS="${GT_AGENT_AUTHORIZED_KEYS:-/root/.ssh/authorized_keys}"

mkdir -p /run/sshd "$KEY_DIR" "$(dirname "$AUTHORIZED_KEYS")"
chmod 700 "$(dirname "$AUTHORIZED_KEYS")"

# Controller keys are per-operation and installed through the signed route; a fresh start begins with none.
: > "$AUTHORIZED_KEYS"
chmod 600 "$AUTHORIZED_KEYS"

# The host key lives in the gt-agent-ssh volume so it survives self-updates: the controller pins it at ADMIT.
if [ ! -f "$HOST_KEY" ]; then
    ssh-keygen -q -t ed25519 -N '' -f "$HOST_KEY"
fi
chmod 600 "$HOST_KEY"
echo "gt-agent: sshd on :$SSH_PORT, host key $(ssh-keygen -lf "$HOST_KEY.pub" | cut -d' ' -f2)"

/usr/sbin/sshd -D -e -p "$SSH_PORT" -h "$HOST_KEY" &
SSHD_PID=$!
python3 /opt/gt-agent/neurons/agent.py &
APP_PID=$!

trap 'kill "$SSHD_PID" "$APP_PID" 2>/dev/null || true' EXIT TERM INT
wait -n "$SSHD_PID" "$APP_PID"
echo "gt-agent: a process exited; stopping the container so it restarts" >&2
exit 1
