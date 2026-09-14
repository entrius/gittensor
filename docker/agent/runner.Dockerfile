# syntax=docker/dockerfile:1.7
# gt-agent runner: the container `gitt up` actually starts. Follows the SIGNED release channel (docker/agent/channel)
# and (re)creates the agent container when the digest it names changes (Lium's Dockerfile.runner + watchtower, in
# one shell loop, minus the mutable tag). Needs only the docker socket.
#
#   docker build -f docker/agent/runner.Dockerfile --build-arg GT_RELEASE_PUB="$(cat docker/agent/keys/gt_release.pub)" \
#       -t entrius/gt-agent-runner:dev .
#
# How `gitt up` runs it (gittensor/agent/launch.py::runner_run_command):
#
#   docker run -d --name gt-agent-runner --restart unless-stopped -v /var/run/docker.sock:/var/run/docker.sock \
#     -e GT_AGENT_CHANNEL_URL=<stable.json url> -e GT_AGENT_CONTAINER_NAME=gt-agent -e GT_AGENT_SSH_PORT=2200 \
#     -e GT_AGENT_MINER_HOTKEY=<ss58> -e GT_AGENT_UPDATE_INTERVAL_S=60 entrius/gt-agent-runner@sha256:<digest>
FROM docker:27-cli
RUN apk add --no-cache openssh-keygen curl jq
# The release-signing PUBLIC key, as the one line of an allowed_signers file (gittensor/agent/channel.py agrees on
# identity and namespace). Baked in: a runner that could be told which key to trust could be told to trust anyone's.
ARG GT_RELEASE_PUB
RUN test -n "$GT_RELEASE_PUB" || { echo 'GT_RELEASE_PUB build-arg (the release public key) is required' >&2; exit 1; } \
    && mkdir -p /etc/gt-agent \
    && printf 'gittensor-release namespaces="gt-agent-channel" %s\n' "$(printf '%s' "$GT_RELEASE_PUB" | awk '{print $1, $2}')" \
        > /etc/gt-agent/allowed_signers
COPY docker/agent/runner.sh /runner.sh
RUN chmod 0755 /runner.sh
ENTRYPOINT ["/runner.sh"]
