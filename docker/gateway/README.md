# gt-gateway

`gitt gateway` in its own container (vault `26` §1, §2): takes das's OpenAI-style requests, picks a healthy leased
instance with a free slot, forwards, streams the answer back, and returns 429 at once when nothing is free. It never
queues.

```bash
docker build -f docker/gateway/Dockerfile \
    --build-arg GATEWAY_UID="$(id -u)" --build-arg GATEWAY_GID="$(id -g)" \
    -t entrius/gt-gateway:dev .

docker run -d --name gt-gateway \
    -p 8790:8790 \
    -v ~/.gittensor/controller:/state:ro \
    -e GT_GATEWAY_KEY="$(cat /path/to/gateway.key)" \
    entrius/gt-gateway:dev \
    --state-dir /state --listen 0.0.0.0:8790 --refresh 3
```

| Mount / env | In the container | What |
|---|---|---|
| controller state dir, **read-only** | `/state` | `instances.json` (written by `gitt controller reconcile`), `tunnels.json` (written by `gitt controller tunnels`), `registry/` (signed entries, re-verified on every read), optional `models_override.json` |
| `GT_GATEWAY_KEY` | env | shared secret; the gateway refuses to start without it |

Dev registries signed with a `DO-NOT-SHIP` key need `--release-pubkey /state/release.pub --allow-dev-keys`.

## How the gateway reaches an instance

Through its tunnel, and only that way. `gt-tunnels` (`docker/controller/README.md`, "gt-tunnels") keeps one SSH
connection per box and a local port per instance, and rewrites `tunnels.json` every ~3 s. On every `--refresh` the
gateway reads it beside `instances.json`: an instance whose tunnel is `up` in a **fresh** file (`written_at` in the
last 10 s) is addressed at the tunnel's `host:port`, for completions, the `/http` passthrough and the `/v1/models`
fetch alike. An older file means no keeper is running: every tunnel in it counts as down. An instance without an up,
fresh tunnel is not routable (its requests get the usual 429), and a missing, unreadable or other-schema
`tunnels.json` means no tunnels; `/healthz` `error` says which. A broken `instances.json` keeps the previous table as
before, still re-addressed by the latest `tunnels.json`.

**Start order:** `gt-tunnels` first, then the gateway (then the controller, as before). The gateway container must
reach the keeper's `--listen-host` (the `gittensor_network` gateway address) on its `--port-range`.

`--allow-direct` (default off) is the rollback switch: an instance with no tunnel is addressed at its record's own
host and port, the previous behaviour. A tunnel that is up still comes first, and an instance whose record says
`bind: private` is never addressed directly (nothing answers there).

## das wiring

das keeps its existing target and adds one header:

```
GATEWAY_URL=http://<gateway host>:8790
X-GT-Gateway-Key: <GT_GATEWAY_KEY>      # on every request; das never forwards user keys
```

- `POST /v1/chat/completions`, `POST /v1/completions`: routed by `model`: the manifest `name`, the entry id
  `name@version`, or the id the runtime reports. The body goes on as sent: `tools`, `tool_choice`,
  `parallel_tool_calls`, `tool_calls` history, `role: tool` and content parts all reach the runtime.
  `max_tokens` / `max_completion_tokens` go on as sent (a positive integer when named) and nothing is set when neither
  is sent: the gateway keeps no output cap of its own, the runtime's limit applies. The gateway only refuses `n != 1`
  and `best_of != 1` (one request, one completion), bodies over `--max-body-bytes`, and remote `image_url` /
  `video_url` (400, "remote media not supported yet"; inline `data:` URLs pass). When the client names the entry and
  the runtime serves a single model under another id, `model` is set to the runtime's id. Runtime errors come back
  with their status and body.
- `GET /v1/models`: each entry's runtime `/v1/models` (from one healthy instance) with the manifest name as `id`,
  `max_output_tokens` from the manifest's `profile` when it names one (else the runtime's own), and
  `models_override.json` (`{"<name or name@version>": {...}}`) merged on top.
- `ANY /http/<name><route>`: `http` front doors, declared routes only, bytes passed through.
- `GET /healthz` (no key): routable instances per entry, `in_flight` per instance (the controller's drain waits on
  it), `tunnels: {"up", "down", "fresh", "written_at"}` from the last `tunnels.json` read (a stale file counts every
  row down), `started_at` (when this gateway process started) and `served` (below). `GET /metrics`: requests, 429s,
  errors, in-flight, `gt_gateway_completion_tokens_total{entry,instance}`, `gt_gateway_tunnel_up{entry,instance}`
  (0|1) and `gt_gateway_tunnels_fresh` (0|1).
- Responses carry `X-GT-Instance` and `X-GT-Entry`, so das's usage row can name the instance.
- A 429 is `{"error": {"type": "capacity", ...}}` with `Retry-After: 1`.

`served` is per instance, cumulative since `started_at` (a restart starts it over; an instance's row is dropped an
hour after it left `instances.json`), what the controller's lease accounting check compares with the runtime's own
counters (`docker/controller/README.md`, "The lease accounting check"):

```json
"served": {"<instance_id>": {"requests": 120, "completion_tokens": 48211,
                            "unaccounted_requests": 3, "unaccounted_allowance_tokens": 12288,
                            "decode_tps_alone_p50": 91.4, "decode_tps_alone_n": 57}}
```

`completion_tokens` sums the runtime's own `usage`. A request whose completion tokens the gateway did not learn (no
`usage`: a stream without `stream_options.include_usage`, a client that left mid-stream, an upstream error, an `http`
front door) is counted in `unaccounted_requests` and adds to `unaccounted_allowance_tokens` the most it could have
made: its `max_tokens` / `max_completion_tokens`, else the runtime's output ceiling (16384, or a higher limit the
manifest names in `profile.max_output_tokens` or `SPARKINFER_MAX_OUTPUT_TOKENS`). `decode_tps_alone_p50` /
`decode_tps_alone_n`: the median `decode_tps` over the last 100 streamed requests that had the instance to themselves
for their whole life (null / 0 before the first). None of this changes what is relayed to the client.

Each request writes one JSON line to stdout: `ts, instance, entry, model, prompt_tokens, completion_tokens, ttft_ms,
total_ms, decode_tps, status, finish_reason, stream`. Tokens come from the runtime's `usage` or are `null`; for
streams, the runtime only sends `usage` when the client asks for `stream_options.include_usage`. Logs go to stderr.

## What this cut does not do

- **No per-instance certificate yet.** The link to an instance is the keeper's SSH connection to its box with HTTP
  inside it; a controller-issued certificate pinned per instance (`26` §10) is later work.
- **No confidentiality on the 5090 fleet.** Miners are root on their boxes and can read prompts there, with or
  without TLS (`26` §10 item 3).
- No remote media download, no Redis (one replica, in-memory slot counters), no Postgres, no user accounts or
  billing (all das).
