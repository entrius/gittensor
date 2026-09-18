# gt-controller

`gitt controller` in its own container (vault `26` §1, §3, §8). Outbound SSH only: no docker socket, no ports, no
chain key. The CA private key, the state directory and the GPU-proof provider are mounts.

```bash
docker build -f docker/controller/Dockerfile --build-arg EXTRA_PIP_PACKAGES="pynacl==1.6.0" \
    --build-arg CONTROLLER_UID="$(id -u)" --build-arg CONTROLLER_GID="$(id -g)" \
    -t entrius/gt-controller:dev .
```

It runs as the image's `controller` user (uid/gid from the build args). Do not pass an arbitrary `--user`: ssh and
ssh-keygen refuse a uid with no passwd entry.

| Mount | In the container | What |
|---|---|---|
| state dir (owned by `CONTROLLER_UID`) | `/state` | `boxes.json`, `instances.json`, `known_hosts`, `nvml_allowlist.json`, `registry/`, `deployments.json`, `controller.json` |
| SSH CA private key | `/secrets/gt_ca` (read-only) | signs the ~5-minute per-visit certificates |
| proof provider source | `/opt/proof` (read-only, on `PYTHONPATH`) | the private `GpuProof` implementation |
| proof binary + secret store | `/opt/proof-dist` (read-only) | e.g. `gt_proof`, `gt_proof.version`, `secret_store.json` |
| pull token (optional) | `/secrets/pull_token` (read-only) | `username:token`, read-only Docker Hub |

The image's entrypoint is **`gitt controller run`**: the controller as one long-lived process.

```bash
docker run -d --name gt-controller --restart unless-stopped --stop-timeout 150 \
    -v ~/.gittensor/controller:/state \
    -v /path/to/gt_ca:/secrets/gt_ca:ro \
    -v /path/to/provider:/opt/proof:ro -e PYTHONPATH=/opt/proof \
    -v /path/to/dist:/opt/proof-dist:ro \
    entrius/gt-controller:dev \
    --state-dir /state --ca-key /secrets/gt_ca \
    --agent-image-digest sha256:<published agent digest> \
    --proof <module:Class> \
    --proof-args secret_store=/opt/proof-dist/secret_store.json \
    --proof-args version=@/opt/proof-dist/gt_proof.version \
    --proof-args binary_path=/opt/proof-dist/gt_proof
```

| `run` flag | Default | What |
|---|---|---|
| `--round-interval` | 1200 s | the two-phase GPU proof over every IDLE / CHECKING card |
| `--build-cmd` | none | shell command after every round (a fresh proof build); the provider is re-read next round |
| `--reconcile-interval` | 30 s | desired replicas vs running; starts and drains run on their own threads |
| `--heartbeat-interval` | 60 s | same card / our container running / card ours alone, per box with a LEASED card |
| `--pull-token-file` | none | installed for each image pull and removed after |
| `--workload-bind` | `private` | where a new workload's port is published (below); `reconcile` takes it too |
| `--release-pubkey`, `--allow-dev-keys` | compiled release key | what registry entries must verify against |
| every `check` / `round` flag | | `--proof`, `--proof-args`, `--agent-image-digest`, `--agent-image-id`, `--proof-image`, `--allowlist`, `--network-target`, `--disk-min-gb`, `--ca-key` |
| `--json` | off | one JSON object per event on stdout instead of log lines on stderr |

The manifest health probe runs per instance on the manifest's own `health.interval_s`. `docker stop` sends SIGTERM: the
loops finish the SSH visit in flight (up to 120 s, hence `--stop-timeout 150`), state is written, and the process exits;
a start still loading is picked up by the next process through its container label.

## Where a workload's port is published

With `--workload-bind private` (the default) a start publishes the workload's port on the box's docker bridge gateway
address only, `-p <bridge gateway>:<host port>:<port>` (from `docker network inspect bridge`, looked up once per box
visit): the agent container and the host are its only callers, and the controller's health probes and canaries already
reach it there. `public` is the previous publish form, `-p <host port>:<port>`.

The setting applies to new starts only; running instances keep theirs, so a fleet changes over one card at a time as
leases cycle. Each instance carries it twice: `bind` (`private` | `public`) on its record in `instances.json` (a
record from before the field reads `public`), and the `io.gittensor.bind` label on its container, which is what a
restarted controller re-adopts it with. A box whose bridge network names no gateway fails the start with that reason.
`bind` is not in the public fleet document.

`run` holds the state directory for its whole life. Beside it, `check`, `round` and `reconcile` refuse ("controller
running, use `gitt controller status`"); `status`, `instances`, `registry show`, `admit` and `deploy` work (the daemon
merges newly admitted boxes and reads `deployments.json` every pass). A one-shot overrides the entrypoint:

```bash
docker run --rm -v ~/.gittensor/controller:/state --entrypoint gitt entrius/gt-controller:dev \
    controller status --state-dir /state
docker run --rm -v ~/.gittensor/controller:/state --entrypoint gitt entrius/gt-controller:dev \
    controller admit <hotkey> --host <ip> --port <port> --state-dir /state
```

`admit <hotkey> --host <ip> --port <port>` and `allowlist add <hotkey>` come first; `remove <hotkey>` forgets a box
that is gone for good (its record and pinned host key; refuses one that still carries an instance); see `gitt controller --help`. A
per-round proof build needs the private build pipeline, which this image deliberately does not carry: run it where
that lives and point `--build-cmd` at it, or rebuild outside and let each round re-read the mounted `dist`.

## gt-tunnels: the traffic path

`gitt controller tunnels` keeps one SSH connection per box that carries an instance (the same certificate CA and
pinned host keys as the controller, a fresh certificate on every connect) and, on it, one local forward per instance:
`<listen host>:<local port>` to the box's docker bridge gateway at the instance's host port, where the workload
answers. Forwards are added and cancelled on the live connection (`ssh -O forward` / `-O cancel`), so one card
cycling leaves the other cards' streams on that box alone. It is its own process, not part of `run`: restarting the
controller leaves the connections that carry traffic up. Run it from the same checkout and state directory as the
controller, under pm2 beside `gt-controller`:

```bash
pm2 start gitt --name gt-tunnels --kill-timeout 45000 -- controller tunnels \
    --state-dir ~/.gittensor/controller --ca-key /path/to/gt_ca --listen-host <gittensor_network gateway>
pm2 logs gt-tunnels                                          # one JSON line per event
gitt controller tunnels --status [--json]                     # what tunnels.json says now
```

| `tunnels` flag | Default | What |
|---|---|---|
| `--listen-host` | `127.0.0.1` | the address local ports listen on: the gateway container's docker network gateway (`gittensor_network`) |
| `--port-range` | `21000-21999` | local ports handed to instances; an instance keeps its port for its whole life, across restarts |
| `--interval` | 3 s | between passes: read `boxes.json` + `instances.json`, connect, add / cancel forwards, probe, write |
| `--ca-key`, `--state-dir` | as `run` | |
| `--status` | | print `tunnels.json` (a table, or `--json`) and exit |

Every pass and every change it writes `<state-dir>/tunnels.json` (tmp + rename), what the gateway routes by:

```json
{"schema": 1, "written_at": 1789760000.0, "listen_host": "<listen host>",
 "tunnels": {"<instance_id>": {"box": "<hotkey>", "host": "<listen host>", "port": 21003,
                              "up": true, "since": 1789759000.0, "error": ""}}}
```

`up`: the box's connection is alive, the forward is registered and one request through the local port got an HTTP
status line back (any status); `since`: when `up` last changed; `error`: why it is not up. Every instance in
`instances.json` gets a tunnel, draining ones included (requests in flight finish through it) until its record is
gone. Each box connects and probes on its own worker, so one slow or unreachable box holds up no other, and the pass
never waits on a box: `written_at` advances every pass (the gateway counts a file older than 10 s as no keeper). A
failed connect is retried after 1 s, doubling to at most 30 s. Every pass also runs `true` on each box over its live
connection (5 s timeout); two failures in a row stop that connection, write its tunnels down at once and reconnect.
That check is on the connection, not a workload: a card starting or stopped never costs the box its connection.
Events (`{"event": "tunnel", "kind": ...}`): `start`, `connect`, `connect_failed`, `link_check`, `forward`, `cancel`,
`up`, `down`, `close`, `stop`. SIGTERM or SIGINT closes every
connection, writes every tunnel down and exits 0; a pass in flight finishes first, hence the pm2 `--kill-timeout`.
One keeper per state directory (`tunnels.lock`). A keeper that was killed outright leaves its connections behind;
the next one closes each before connecting the box again.

## The public fleet document

`run` writes `<state-dir>/public/fleet.json` every 30 s and with every scorecard (tmp + rename; `gitt controller
publish` writes it once). It is the only file meant to leave the state directory: das-gittensor serves it as
`GET /compute/fleet` through a **read-only bind mount of `public/` alone**, never of the state directory (the CA key
and every box's address live beside it). `public/` is 0755 and the file 0644 for that reader. The validator does not
read it.

| Field (`"schema": 1`) | What |
|---|---|
| `generated_at`, `network`, `netuid` | when it was written; a reader calls it stale after 3 x `controller.publish_interval_s` |
| `controller` | `running`, `round_n`, `last_round_at`, `round_interval_s`, `publish_interval_s` |
| `scorecard` | `sha256`, `issued_at`, `valid_until`, `valid`, `recycle_share` (null before the first scorecard) |
| `rates.<gpu_type>` | `idle_usd_per_card_hour`, `leased_usd_per_card_hour`, `source` (`scorecard`: what the last one implied; `table`: `fleet_pay.json` targets) |
| `oracle` | `tao_usd`, `alpha_tao`, `held` |
| `totals` | `boxes`, `cards`, `cards_by_state` |
| `boxes[]` | `hotkey`, `uid` (as discovery last read the metagraph; null for a hotkey not registered, or without `--discover`), `status`, `standing`, `gpu_type`, `card_count`, `last_check_at`, `last_failed[]` (check names), `bench_until`, `benched_reason`, `pay{weight, idle_h, leased_h, usd_window}`, `last_event{at, kind}` |
| `boxes[].cards[]` | `card` (first 12 hex of sha256 of the GPU UUID), `state`, `since`; with an instance on it: `workload`, `image` (repo:tag), `leased_at`, `uptime_s`, `healthy`, `draining`, `heartbeat_misses`, `last_heartbeat_at` |

Never in it: a host, IP or port, a port map, container or image ids, raw GPU UUIDs, host keys, the NVML md5, file
paths, proof provider or version ids, error text. `publish.py` names every field it copies;
`tests/controller/test_publish.py` sets all of those on a fixture and asserts none reaches the output.
