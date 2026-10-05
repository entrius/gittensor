#!/usr/bin/env bash
# Dev keypairs for building and running the agent locally. Both carry DO-NOT-SHIP in the comment: the agent
# entrypoint refuses to start on a CA tagged that way unless GT_AGENT_ALLOW_DEV_KEYS=1, so an image built on these
# cannot quietly end up on a real miner box. Real keys are generated offline and never live here (see README.md).
#
#   docker/agent/keys/make-dev-keys.sh
#   docker build -f docker/agent/Dockerfile --build-arg GT_CA_PUB="$(cat docker/agent/keys/gt_ca.pub)" -t entrius/gt-agent:dev .
set -euo pipefail
cd "$(dirname "$0")"
for name in gt_ca gt_release; do
    if [ -f "$name" ]; then
        echo "$name exists; not overwriting" >&2
        continue
    fi
    ssh-keygen -q -t ed25519 -N '' -C "${name//_/-}-dev DO-NOT-SHIP" -f "$name"
    echo "wrote $name / $name.pub ($(ssh-keygen -lf "$name.pub" | cut -d' ' -f2))"
done
