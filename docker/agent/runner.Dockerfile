# syntax=docker/dockerfile:1.7
# gt-agent runner: the container `gitt up` actually starts. Follows the agent image's pinned tag on our registry
# and (re)creates the agent container when the image digest changes (Lium's Dockerfile.runner + watchtower, in
# one shell loop). Needs only the docker socket.
#
#   docker build -f docker/agent/runner.Dockerfile -t entrius/gt-agent-runner:dev .
#
# How `gitt up` runs it (gittensor/agent/launch.py::runner_run_command):
#
#   docker run -d --name gt-agent-runner --restart unless-stopped -v /var/run/docker.sock:/var/run/docker.sock \
#     -e GT_AGENT_IMAGE=entrius/gt-agent:stable -e GT_AGENT_CONTAINER_NAME=gt-agent \
#     -e GT_AGENT_SSH_PORT=2200 -e GT_AGENT_HTTP_PORT=8200 -e GT_AGENT_MINER_HOTKEY=<ss58> \
#     -e GT_AGENT_UPDATE_INTERVAL_S=60 entrius/gt-agent-runner:stable
FROM docker:27-cli
COPY docker/agent/runner.sh /runner.sh
RUN chmod 0755 /runner.sh
ENTRYPOINT ["/runner.sh"]
