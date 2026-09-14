#!/bin/sh
# gt-agent runner loop (busybox sh). Every GT_AGENT_UPDATE_INTERVAL_S: fetch the signed release channel, verify the
# signature against the release key baked into this image, and if the agent image digest it names differs from the
# running agent's (or the agent is not running) pull that digest and recreate the agent. A channel that fails to
# fetch or verify changes nothing: whatever is running keeps running. The `docker run` below is the agent line —
# keep it identical to gittensor/agent/launch.py::agent_run_command (a test checks the flags).
set -u

CHANNEL_URL="${GT_AGENT_CHANNEL_URL:?GT_AGENT_CHANNEL_URL is required}"
NAME="${GT_AGENT_CONTAINER_NAME:-gt-agent}"
SSH_PORT="${GT_AGENT_SSH_PORT:-2200}"
MINER_HOTKEY="${GT_AGENT_MINER_HOTKEY:-}"
INTERVAL="${GT_AGENT_UPDATE_INTERVAL_S:-60}"
VOLUME="${GT_AGENT_SSH_VOLUME:-gt-agent-ssh}"
ALLOWED_SIGNERS="${GT_AGENT_ALLOWED_SIGNERS:-/etc/gt-agent/allowed_signers}"
SIGNER_IDENTITY="${GT_AGENT_SIGNER_IDENTITY:-gittensor-release}"
SIGN_NAMESPACE="${GT_AGENT_SIGN_NAMESPACE:-gt-agent-channel}"
AGENT_REPO="${GT_AGENT_IMAGE_REPO:-entrius/gt-agent}"
WORK=/tmp/gt-agent-runner

log() { echo "gt-agent-runner: $*"; }

# Prints the agent image ref (repo@sha256:...) from a verified channel, or nothing.
channel_agent_ref() {
    mkdir -p "$WORK"
    if ! curl -fsSL --max-time 30 -o "$WORK/channel.json" "$CHANNEL_URL" \
        || ! curl -fsSL --max-time 30 -o "$WORK/channel.json.sig" "$CHANNEL_URL.sig"; then
        log "channel fetch failed ($CHANNEL_URL)"
        return 1
    fi
    if ! ssh-keygen -Y verify -f "$ALLOWED_SIGNERS" -I "$SIGNER_IDENTITY" -n "$SIGN_NAMESPACE" \
        -s "$WORK/channel.json.sig" < "$WORK/channel.json" >/dev/null 2>&1; then
        log "channel signature does NOT verify; ignoring it"
        return 1
    fi
    ref="$(jq -r '.agent // empty' "$WORK/channel.json")"
    case "$ref" in
        "$AGENT_REPO@sha256:"*) echo "$ref" ;;
        *) log "channel names '$ref', not a digest-pinned $AGENT_REPO; ignoring it"; return 1 ;;
    esac
}

start_agent() {
    image="$1"
    digest="${image#*@}"
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" --restart unless-stopped --privileged --pid host --gpus all \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v "$VOLUME:/var/lib/gt-agent" \
        -p "$SSH_PORT:$SSH_PORT" \
        -e "GT_AGENT_SSH_PORT=$SSH_PORT" \
        -e "GT_AGENT_MINER_HOTKEY=$MINER_HOTKEY" \
        -e "GT_AGENT_IMAGE=$image" \
        -e "GT_AGENT_IMAGE_DIGEST=$digest" \
        -e NVIDIA_DRIVER_CAPABILITIES=all \
        "$image"
}

# Only OUR old agent images go: never a host-wide prune (the miner's other images are not ours to delete).
prune_old_agent_images() {
    keep="$1"
    for id in $(docker images --filter "reference=$AGENT_REPO" --format '{{.ID}}' | sort -u); do
        [ "$id" = "$keep" ] || docker rmi "$id" >/dev/null 2>&1 || true
    done
}

log "following $CHANNEL_URL for container $NAME every ${INTERVAL}s"
while :; do
    if want_ref="$(channel_agent_ref)"; then
        if ! docker pull -q "$want_ref" >/dev/null 2>&1; then
            log "pull of $want_ref failed; keeping what is running"
        else
            want="$(docker image inspect --format '{{.Id}}' "$want_ref" 2>/dev/null || echo none)"
            have="$(docker inspect --format '{{.Image}}' "$NAME" 2>/dev/null || echo none)"
            running="$(docker inspect --format '{{.State.Running}}' "$NAME" 2>/dev/null || echo false)"
            if [ "$want" != none ] && { [ "$want" != "$have" ] || [ "$running" != true ]; }; then
                log "(re)starting $NAME: image $have -> $want ($want_ref), running=$running"
                if start_agent "$want_ref"; then
                    prune_old_agent_images "$want"
                else
                    log "docker run failed; retrying in ${INTERVAL}s"
                fi
            fi
        fi
    fi
    sleep "$INTERVAL"
done
