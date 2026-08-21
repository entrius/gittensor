# Gittensor Serving Runtime Contract — v0 (UNSTABLE)

**Audience:** maintainers of inference runtimes that want Gittensor compute miners to run them
(today: [`sparkinfer_server`](https://github.com/gittensor-ai-lab/sparkinfer)), and miners
checking that their box is ready.

**Stability:** v0 is what the `feat/serving-scaffold` validator and miner code actually require.
Expect it to change while step 0 is in development; every change bumps the version header and is
called out in the changelog at the bottom. A runtime is "conformant" against a specific version.

**Check it:** `uv run python scripts/check_serving_runtime.py --base-url http://127.0.0.1:8080 --model-id <id>`
runs every MUST below and prints PASS/FAIL per item plus the determinism and overload measurements.

---

## 1. Why a contract

The miner (`neurons/serving_miner.py`) and validator never know which runtime is behind a miner.
They speak one narrow, OpenAI-chat-shaped interface (`gittensor/serving/backends.py`,
`OpenAICompatBackend`). Any server that satisfies this document is plug-and-play: add a release to
the serving loadout, build an audit bank for it, done. The contract exists so that new
runtimes/optimisations can be added by their own maintainers without touching subnet code.

## 2. Release = runtime build + model artifact

A **release** is the pair `(runtime_pin, model_id)`:

- `runtime_pin` — `<org>/<repo>@<commit-or-tag>` of the runtime build. Miners MUST run exactly
  this build; the audit bank is generated from it.
- `model_id` — the string the runtime reports in `/v1/models` and in `choices[].model` /
  top-level `model`. It MUST identify weights + quantisation unambiguously
  (e.g. `qwen3.6-35b-a3b` ↔ Qwen3.6-35B-A3B UD-Q4_K_M as shipped by sparkinfer).

Changing either side is a new release and requires a new audit bank.

## 3. HTTP surface

All endpoints are plain HTTP on the miner's loopback (the miner neuron is the only client; it is
reached over the Bittensor axon). JSON request/response bodies.

| Endpoint | Level | Used by |
|---|---|---|
| `POST /v1/chat/completions` | **MUST** | audits and user traffic (identical shape) |
| `GET /v1/models` | **MUST** | loadout `model_id` check, conformance |
| `GET /v1/capacity` | SHOULD | future KV-aware routing; conformance reports it |
| `GET /metrics` (Prometheus) | MAY | miner ops |
| `GET /health` | SHOULD | entrypoint readiness |

### 3.1 `POST /v1/chat/completions`

Request fields the subnet sends (others are never sent and MAY be rejected):

```json
{
  "model": "<model_id>",
  "messages": [{"role": "system|user|assistant", "content": "..."}],
  "max_tokens": 64,
  "temperature": 0,
  "stream": false,
  "logprobs": true,
  "top_logprobs": 1
}
```

Requirements:

- **R1 Greedy decode.** `temperature: 0` MUST mean greedy argmax decoding. No sampling,
  no `top_p`/`top_k` fallback, no seed dependence.
- **R2 Token logprobs.** When `logprobs: true`, the response MUST include
  `choices[0].logprobs.content`: an array with one entry per generated token, each with `token`
  (string) and `logprob` (float, natural log, ≤ 0). Entry count MUST equal
  `usage.completion_tokens`. `top_logprobs` inside each entry MAY be present and is ignored.
- **R3 Response shape.** Standard OpenAI: `choices[0].message.content` (string),
  `choices[0].finish_reason` (`stop` | `length`), `usage.{prompt_tokens,completion_tokens,total_tokens}`,
  top-level `model`.
- **R4 `max_tokens` honoured exactly.** Generation MUST stop at `max_tokens` with
  `finish_reason: "length"`.
- **R5 Timing fields (SHOULD, additive, top level).** `ttft_ms` (first-token latency),
  `generation_ms` (wall time for the whole generation), `decode_tps` (tokens/s after first token).
  Floats. If absent the validator falls back to its own round-trip measurement, which is strictly
  worse for the miner's latency credit.
- **R6 Overload = 429, never queue.** When the runtime has no free slot (concurrency or KV
  budget), it MUST return HTTP 429 immediately. It MUST NOT hold the request in an internal queue.
  The subnet's gateway promise is "reserve or 429", so a queueing runtime poisons the latency
  signal for everyone behind it.
- **R7 Streaming (MAY).** `stream: true` is not sent in v0. Runtimes SHOULD support it for the
  later streaming transport.

### 3.2 `GET /v1/models`

OpenAI shape: `{"data": [{"id": "<model_id>", ...}]}`. Exactly one model per runtime process
in v0 (one release per GPU). The `id` MUST equal the loadout `model_id`.

### 3.3 `GET /v1/capacity` (SHOULD)

```json
{"active_requests": 1, "max_concurrent": 4, "free_kv_blocks": 1234, "total_kv_blocks": 4096}
```

Field names follow sparkinfer; additional fields are fine. Not consumed by the validator in v0.

## 4. Determinism requirement (the important one)

**D1.** For a fixed release on the target hardware class (RTX 5090 in v0), repeated greedy
decodes of the same request MUST produce the same token sequence for the overwhelming majority
of prompts, and per-token logprobs MUST be numerically stable.

This is what makes verification possible without validators owning GPUs: the validator holds a
bank of reference `(prompt → tokens, logprobs)` generated by a trusted copy of the release and
checks miners against it (`gittensor/serving/audit.py`). The thresholds
(`SERVING_AUDIT_MIN_PREFIX_AGREEMENT`, `SERVING_AUDIT_MAX_MEAN_ABS_LOGPROB_DIFF` in
`gittensor/constants.py`) are **calibrated from the runtime's own measured stability**
(`scripts/build_serving_audit_bank.py --repeat N`; the conformance checker reports the same
numbers). A runtime whose greedy output drifts a lot across identical runs (batching-dependent
kernels, non-deterministic reductions, dynamic quant paths) forces looser thresholds, which
makes cheating easier for *everyone* on the subnet. Runtimes SHOULD therefore:

- use deterministic kernels / reductions for the decode path where available;
- make batching not change per-sequence numerics, or document the drift;
- expose a build flag or env var to pin any remaining source of nondeterminism.

Acceptance in v0: over a 200-prompt sample, p05 of prefix agreement across 3 repeats ≥ 0.90 and
p95 of mean |Δlogprob| ≤ 0.25 (provisional; the checker prints the observed values so maintainers
can see where they land).

## 5. Identity & provenance

- **P1.** Release binaries SHOULD be attestable (e.g. GitHub Artifact Attestations,
  `gh attestation verify`). Miners pin by commit; attestation lets the subnet later require it.
- **P2.** The runtime SHOULD report its own build identity (commit / version) somewhere
  machine-readable (`/v1/models` extra field, `/health`, or `/metrics`). v0 does not verify it,
  later versions will bind it into the READY check.

## 6. Operational

- **O1.** One process serves one release on one GPU. Multi-GPU / multi-model per process is out
  of scope for v0.
- **O2.** Startup MUST be unattended: a single command that downloads/verifies the pinned model
  and serves (sparkinfer: `server/run.sh --download --port 8080`). The miner entrypoint
  (`scripts/serving-miner-entrypoint.sh`) assumes the server is already listening.
- **O3.** Requests are bounded: the subnet caps `max_tokens` at `SERVING_MAX_TOKENS` (1024) and
  prompts at whatever the release's certified context is. The runtime SHOULD reject
  over-context prompts with 400, not truncate silently.

## 7. What the subnet promises back

- The validator always requests `logprobs` so audits and organic traffic are indistinguishable
  on the wire. Runtime authors should not special-case either.
- Only the fields above are read. Extra fields are ignored, never an error.
- Contract changes are versioned here and announced before validators enforce them.

## 8. Conformance matrix

| Runtime | Contract version | Status | Notes |
|---|---|---|---|
| `gittensor-ai-lab/sparkinfer` `sparkinfer_server` | v0 | conformant (verified by inspection 2026-08-20; checker run pending 5090 session) | default release: Qwen3.6-35B-A3B UD-Q4_K_M |

## Changelog

- **v0 (2026-08-21)** — initial contract extracted from `feat/serving-scaffold` step 0.
