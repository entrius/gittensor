#!/bin/sh
# gt-agent runner loop (busybox sh). Every GT_AGENT_UPDATE_INTERVAL_S: pull the pinned tag; if the image id the
# tag resolves to differs from the running agent's, or the agent is not running, recreate it. The `docker run`
# below is the agent line — keep it identical to gittensor/agent/launch.py::agent_run_command (a test checks
# the flags).
set -u

IMAGE="${GT_AGENT_IMAGE:?GT_AGENT_IMAGE is required}"
NAME="${GT_AGENT_CONTAINER_NAME:-gt-agent}"
SSH_PORT="${GT_AGENT_SSH_PORT:-2200}"
HTTP_PORT="${GT_AGENT_HTTP_PORT:-8200}"
MINER_HOTKEY="${GT_AGENT_MINER_HOTKEY:-}"
INTERVAL="${GT_AGENT_UPDATE_INTERVAL_S:-60}"
VOLUME="${GT_AGENT_SSH_VOLUME:-gt-agent-ssh}"

log() { echo "gt-agent-runner: $*"; }

start_agent() {
    digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "$IMAGE" 2>/dev/null || true)"
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" --restart unless-stopped --privileged --pid host --gpus all \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v "$VOLUME:/var/lib/gt-agent" \
        -p "$SSH_PORT:$SSH_PORT" \
        -p "$HTTP_PORT:$HTTP_PORT" \
        -e "GT_AGENT_SSH_PORT=$SSH_PORT" \
        -e "GT_AGENT_HTTP_PORT=$HTTP_PORT" \
        -e "GT_AGENT_MINER_HOTKEY=$MINER_HOTKEY" \
        -e "GT_AGENT_IMAGE=$IMAGE" \
        -e "GT_AGENT_IMAGE_DIGEST=$digest" \
        -e NVIDIA_DRIVER_CAPABILITIES=all \
        "$IMAGE"
}

log "following $IMAGE for container $NAME every ${INTERVAL}s"
while :; do
    if ! docker pull -q "$IMAGE" >/dev/null 2>&1; then
        log "pull of $IMAGE failed; keeping what is running"
    fi
    want="$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null || echo none)"
    have="$(docker inspect --format '{{.Image}}' "$NAME" 2>/dev/null || echo none)"
    running="$(docker inspect --format '{{.State.Running}}' "$NAME" 2>/dev/null || echo false)"
    if [ "$want" = none ]; then
        log "$IMAGE is not available locally yet"
    elif [ "$want" != "$have" ] || [ "$running" != true ]; then
        log "(re)starting $NAME: image $have -> $want, running=$running"
        if start_agent; then
            docker image prune -f >/dev/null 2>&1 || true
        else
            log "docker run failed; retrying in ${INTERVAL}s"
        fi
    fi
    sleep "$INTERVAL"
done
