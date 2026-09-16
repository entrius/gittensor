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
| controller state dir, **read-only** | `/state` | `instances.json` (written by `gitt controller reconcile`), `registry/` (signed entries, re-verified on every read), optional `models_override.json` |
| `GT_GATEWAY_KEY` | env | shared secret; the gateway refuses to start without it |

Dev registries signed with a `DO-NOT-SHIP` key need `--release-pubkey /state/release.pub --allow-dev-keys`.

## das wiring

das keeps its existing target and adds one header:

```
GATEWAY_URL=http://<gateway host>:8790
X-GT-Gateway-Key: <GT_GATEWAY_KEY>      # on every request; das never forwards user keys
```

- `POST /v1/chat/completions`, `POST /v1/completions`: routed by `model`: the manifest `name`, the entry id
  `name@version`, or the id the runtime reports. The body goes on as sent: `tools`, `tool_choice`,
  `parallel_tool_calls`, `tool_calls` history, `role: tool` and content parts all reach the runtime. The gateway only
  clamps `max_tokens` / `max_completion_tokens` to 4096 (and sets `max_tokens: 4096` when neither is sent), refuses
  `n != 1`, bodies over `--max-body-bytes`, and remote `image_url` / `video_url` (400, "remote media not supported
  yet"; inline `data:` URLs pass). When the client names the entry and the runtime serves a single model under another
  id, `model` is set to the runtime's id. Runtime errors come back with their status and body.
- `GET /v1/models`: each entry's runtime `/v1/models` (from one healthy instance) with the manifest name as `id`,
  `max_output_tokens` capped at 4096, and `models_override.json` (`{"<name or name@version>": {...}}`) merged on top.
- `ANY /http/<name><route>`: `http` front doors, declared routes only, bytes passed through.
- `GET /healthz` (no key): routable instances per entry. `GET /metrics`: requests, 429s, errors, in-flight.
- Responses carry `X-GT-Instance` and `X-GT-Entry`, so das's usage row can name the instance.
- A 429 is `{"error": {"type": "capacity", ...}}` with `Retry-After: 1`.

Each request writes one JSON line to stdout: `ts, instance, entry, model, prompt_tokens, completion_tokens, ttft_ms,
total_ms, decode_tps, status, finish_reason, stream`. Tokens come from the runtime's `usage` or are `null`; for
streams, the runtime only sends `usage` when the client asks for `stream_options.include_usage`. Logs go to stderr.

## What this cut does not do

- **Plain HTTP to instances.** No TLS and no per-instance secret yet. The target (`26` §10) is HTTPS with a
  controller-issued certificate pinned per instance, plus a per-instance secret so only the gateway can use the port.
  Until then, anyone on the network path can read or inject traffic between the gateway and a miner box.
- **No confidentiality on the 5090 fleet.** Miners are root on their boxes and can read prompts there, with or
  without TLS (`26` §10 item 3).
- No remote media download, no Redis (one replica, in-memory slot counters), no Postgres, no user accounts or
  billing (all das).
