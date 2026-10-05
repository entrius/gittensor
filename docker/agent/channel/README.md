# Agent release channel

The runner on every miner box follows `stable.json` here (raw GitHub URL in
`gittensor/agent/config.py::AGENT_CHANNEL_URL`), never a mutable image tag. A release is a new `stable.json` plus
`stable.json.sig`, an OpenSSH signature by the release key (`docker/agent/keys`, namespace `gt-agent-channel`).
A channel that fails to verify is ignored: whatever is running keeps running.

```json
{
  "agent":  "entrius/gt-agent@sha256:<64 hex>",
  "runner": "entrius/gt-agent-runner@sha256:<64 hex>",
  "version": "5.1.0",
  "issued_at": 1789000000
}
```

`sign.sh` writes both files from the two digests; CI runs it with the release private key from its secrets after
pushing the images. Verify by hand:

```
ssh-keygen -Y verify -f <(echo 'gittensor-release namespaces="gt-agent-channel" '"$(cut -d' ' -f1,2 gt_release.pub)") \
  -I gittensor-release -n gt-agent-channel -s stable.json.sig < stable.json
```

No `stable.json` is committed yet: there is no published agent image (vault `24` §3 WS-A, "leave the actual images
unpublished").
