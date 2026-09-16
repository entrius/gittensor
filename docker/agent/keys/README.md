# Agent trust anchors

Two keypairs, both compiled into images at build time (vault `26` §5–6). Nothing in this directory is committed
except this file and the dev-key script.

| Key | Public half goes into | Private half lives in | Rotation |
|---|---|---|---|
| `gt_ca` — the SSH certificate authority | `entrius/gt-agent` (`--build-arg GT_CA_PUB`, sshd `TrustedUserCAKeys`) | the controller container only (`gittensor/controller/ssh`) | ship an agent release |
| `gt_release` — the release-signing key | `entrius/gt-agent-runner` (`--build-arg GT_RELEASE_PUB`) and `gittensor/agent/config.py::RELEASE_PUBKEY_OPENSSH` | CI secrets (`docker/agent/channel/sign.sh`) | ship an agent release |

**Local development:** `./make-dev-keys.sh` writes both pairs here with `DO-NOT-SHIP` in the comment. An agent
image built on the dev CA refuses to start unless `GT_AGENT_ALLOW_DEV_KEYS=1` (`gitt up --no-update
--allow-dev-keys`), so a dev image cannot be run on a box facing the internet by accident.

**Production:** generate the real pairs offline (`ssh-keygen -t ed25519 -C gittensor-ca`, `-C gittensor-release`),
put the private halves where the table says, and pass the public halves as build args in CI. Never commit either.
