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
| `--release-pubkey`, `--allow-dev-keys` | compiled release key | what registry entries must verify against |
| every `check` / `round` flag | | `--proof`, `--proof-args`, `--agent-image-digest`, `--agent-image-id`, `--proof-image`, `--allowlist`, `--network-target`, `--disk-min-gb`, `--ca-key` |
| `--json` | off | one JSON object per event on stdout instead of log lines on stderr |

The manifest health probe runs per instance on the manifest's own `health.interval_s`. `docker stop` sends SIGTERM: the
loops finish the SSH visit in flight (up to 120 s, hence `--stop-timeout 150`), state is written, and the process exits;
a start still loading is picked up by the next process through its container label.

`run` holds the state directory for its whole life. Beside it, `check`, `round` and `reconcile` refuse ("controller
running, use `gitt controller status`"); `status`, `instances`, `registry show`, `admit` and `deploy` work (the daemon
merges newly admitted boxes and reads `deployments.json` every pass). A one-shot overrides the entrypoint:

```bash
docker run --rm -v ~/.gittensor/controller:/state --entrypoint gitt entrius/gt-controller:dev \
    controller status --state-dir /state
docker run --rm -v ~/.gittensor/controller:/state --entrypoint gitt entrius/gt-controller:dev \
    controller admit <hotkey> --host <ip> --port <port> --state-dir /state
```

`admit <hotkey> --host <ip> --port <port>` and `allowlist add <hotkey>` come first; see `gitt controller --help`. A
per-round proof build needs the private build pipeline, which this image deliberately does not carry: run it where
that lives and point `--build-cmd` at it, or rebuild outside and let each round re-read the mounted `dist`.

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
| `boxes[]` | `hotkey`, `uid` (null: the controller reads no UIDs), `status`, `standing`, `gpu_type`, `card_count`, `last_check_at`, `last_failed[]` (check names), `bench_until`, `benched_reason`, `pay{weight, idle_h, leased_h, usd_window}`, `last_event{at, kind}` |
| `boxes[].cards[]` | `card` (first 12 hex of sha256 of the GPU UUID), `state`, `since`; with an instance on it: `workload`, `image` (repo:tag), `leased_at`, `uptime_s`, `healthy`, `draining`, `heartbeat_misses`, `last_heartbeat_at` |

Never in it: a host, IP or port, a port map, container or image ids, raw GPU UUIDs, host keys, the NVML md5, file
paths, proof provider or version ids, error text. `publish.py` names every field it copies;
`tests/controller/test_publish.py` sets all of those on a fixture and asserts none reaches the output.
