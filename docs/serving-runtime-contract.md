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

- `model_sha256` — digest of the exact model file. Upstream lockfiles are not enough: HF repos get
  re-uploaded under the same path (unsloth's Qwen3.6 GGUF changed under sparkinfer `1b8b962`'s
  `reference.lock` on 2026-08-21, and its default start then re-downloads forever). The subnet pins
  the digest itself and passes it to the runtime (`MODEL_SHA256` for sparkinfer's `run.sh`).

Changing any of the three is a new release and requires recalibration.

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
- **R5 Timing fields (SHOULD, additive).** `ttft_ms` (first-token latency), `generation_ms`
  (wall time for the whole generation), `decode_tps` (tokens/s after first token). Floats, either
  top level or inside `usage` (sparkinfer `1b8b962` nests them in `usage`). If absent the validator
  falls back to its own round-trip measurement, which is strictly worse for the miner's latency credit.
- **R6 Overload = 429, never queue.** When the runtime has no free slot (concurrency or KV
  budget), it MUST return HTTP 429 immediately. It MUST NOT hold the request in an internal queue.
  The subnet's gateway promise is "reserve or 429", so a queueing runtime poisons the latency
  signal for everyone behind it.
- **R7 Streaming (MAY).** `stream: true` is not sent in v0. Runtimes SHOULD support it for the
  later streaming transport.
- **R8 Teacher-forced scoring (SHOULD in v0, MUST in v1).** Given a prompt and a *supplied*
  continuation, return the per-token logprob of each continuation token under the model without
  generating (OpenAI legacy `POST /v1/completions` with `echo: true, logprobs: 1, max_tokens: 0`,
  or an equivalent documented endpoint). This lets a validator audit *any* text — another miner's
  answer, a perturbed reference, organic traffic — so there is nothing finite to memorise. It is
  the audit primitive the subnet moves to after step 0.

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

**Measured, sparkinfer `1b8b962` on an RTX 5090 (2026-08-21, 30 prompts × 4 runs, sequential,
batch of 1, DFlash off):** prefix agreement min 0.00 / p05 0.06 / median 0.71; logprob drift on the
agreed prefix max 0.08 / p95 0.04. Direct probe: the logprob of the *same token at the same
position* moves by ~0.25–0.4 nats between identical runs, so greedy flips whenever the top-2 are
within that band (observed at token 2 and token 5 of ordinary prompts). The sampler is argmax with
no RNG, as upstream says; the *logits* are not reproducible. This fails D1 as written and is the
top open item with the maintainer (see §9). Until the runtime is reproducible, validators must use
a divergence-tolerant comparison (teacher-forced scoring, R8 — which upstream already has as the
`qwen3_gguf_score` binary but does not expose over HTTP).

### 4.1 How validators use the contract: the reference

A validator verifies a release against **a conformant copy it controls** — the same image miners
run, either on the validator's own GPU (compose profile `reference`) or on a rented 5090 kept warm
behind `--api-key` (`reference_url` + `reference_api_key`, or `SERVING_REFERENCE_URL` /
`SERVING_REFERENCE_API_KEY`). The validator itself needs no GPU. Every audit draws a fresh prompt, asks the reference for the honest answer, and
compares the miner's tokens/logprobs to it. Because the reference is live, any prompt can be
audited (including mirrored organic traffic and, via R8, any text), so a miner cannot learn a
fixed answer set. A validator without a GPU falls back to a *bank snapshot* of a reference
(`audit_bank`, built by `scripts/build_serving_audit_bank.py`), which is finite and must be
rotated. Either way the verifier code is identical and release-agnostic: adding a model or
runtime to the subnet is "run a conformant copy + add a release entry", nothing more.

### 4.2 Planted-cheater experiment (2026-08-22, rented RTX 5090)

Question: given the D1 jitter above, can the verifier still tell an honest miner from a cheaper
one? Method: `scripts/serving_cheat_experiment.py` — 40 fresh prompts × 64 tokens recorded from the
honest reference (sparkinfer `1b8b962`, UD-Q4_K_M), then the same prompts scored against each
candidate. Metrics per prompt: prefix agreement (what `verify_response` uses), mean |Δlogprob| over
the agreed prefix, and *positional overlap* (fraction of positions where the token matches,
ignoring the first divergence).

| candidate | runtime | prefix agreement med (p05–p95) | positional overlap med | drift med / max | ms med |
|---|---|---|---|---|---|
| honest, re-run | sparkinfer | 0.52 (0.02–1.00) | 0.56 | 0.016 / 0.195 | 163 |
| honest, under 8 concurrent background requests | sparkinfer | 0.47 (0.06–1.00) | 0.65 | 0.016 / 0.179 | 163 |
| UD-Q4_K_S (≈5 % smaller) | sparkinfer | 0.33 (0.08–1.00) | 0.40 | 0.033 / 0.108 | 161 |
| UD-Q4_K_M (honest weights) | llama.cpp | 0.20 (0.00–1.00) | 0.24 | 0.045 / 0.154 | 607 |
| UD-Q2_K_XL | llama.cpp | 0.09 (0.00–0.33) | 0.11 | 0.061 / 0.233 | 567 |

Findings:

- **Single-audit pass/fail cannot work against this pin.** With the current constants (prefix
  ≥ 0.80, drift ≤ 0.50) the *honest* miner passes only 37 % of audits; the Q2 cheater passes 0 %.
  Their per-audit distributions overlap (honest p05 0.02 vs cheater p95 0.33).
- **Aggregating over a window does work.** Bootstrapping the per-prompt samples, with the
  threshold set at a 1 % false-positive rate on honest miners (metric = mean positional overlap
  over the last *k* audits): Q2_K_XL/llama.cpp is caught 83 % at k = 5, 99.8 % at k = 10, 100 % at
  k = 20; honest-weights/llama.cpp (runtime substitution) 27 % at k = 5, 61 % at k = 10, 93 % at
  k = 20. Q4_K_S under sparkinfer is the hardest case and needs a longer window still (summary only —
  the per-prompt rows for that run were lost at teardown).
- **Batching does not widen the drift** (row 2 ≈ row 1), so miners can be audited while serving.
- **Logprob drift over the agreed prefix is a weak separator on its own** (0.016 honest vs 0.061
  Q2) because the prefix is short whenever the models disagree; it becomes the primary signal only
  with teacher-forced scoring (R8), where every position is compared.
- **sparkinfer loads only F32/F16/Q8_0/Q4_K/Q5_K/Q6_K tensors** (`ggml_dequant_supported`), so a
  cheaper quant *under sparkinfer* is limited to Q4_K_S; a cheap cheater necessarily uses another
  runtime, which is both slower (~3.7×) and numerically farther away.
- sparkinfer's missing first-token logprob (§9 #3) shifts cross-runtime comparisons by one token;
  the experiment aligns for it (`--drop-first`). Verifier code must not assume position 0 is present.

Consequence for the verifier (step 0, implemented): `AuditWindow` in `gittensor/serving/audit.py`
keeps the last `SERVING_AUDIT_WINDOW = 20` positional overlaps per (hotkey, release) and passes the
miner when their mean clears `SERVING_AUDIT_OVERLAP_THRESHOLDS` for that many audits (the 1 % FP
column above, interpolated). Per-audit verdicts are telemetry only. Raw data, reproduction steps
and the offline `analyze` command live in `docs/serving-experiments/2026-08-22-planted-cheater/`;
`tests/validator/test_serving.py` replays that data so the constants cannot drift from it. Revisit
once R8 lands.

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
- **O2b.** The subnet builds and publishes the image for every blessed release itself
  (`docker/sparkinfer.Dockerfile`, workflow `sparkinfer-image.yml` → `entrius/sparkinfer:<commit>`),
  from the upstream repo at the pinned commit — no fork, no dependence on upstream publishing images.
  Validators run that image as the reference (compose profile `reference`), miners run it to serve.
  Runtimes SHOULD keep an unattended source build working (`cmake … -DBUILD_SERVER=ON`) so this
  pipeline stays a thin wrapper; an upstream Dockerfile is welcome but not required.
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
| `gittensor-ai-lab/sparkinfer` `sparkinfer_server` `1b8b962` | v0 | **checker run on RTX 5090 2026-08-21:** all HTTP MUSTs pass (after tolerating the first-token logprob gap); **D1 fails** — see §4 and §9 | default release: Qwen3.6-35B-A3B UD-Q4_K_M sha256 `ac0e2c11…`; image `entrius/sparkinfer:1b8b962` built by us |

## 9. Open items with sparkinfer (as of `1b8b962`)

1. **Logit reproducibility (D1).** Same prompt, batch of 1, sequential: per-token logprobs vary
   by ~0.3 nats; greedy forks early. Need a deterministic decode path (or a flag) for the
   blessed release.
2. **Expose teacher-forced scoring over HTTP (R8).** `/v1/completions` with `echo`+`logprobs`, or a
   `/v1/score` wrapping `qwen3_gguf_score`. This is what makes verification robust to (1).
3. **First-token logprob missing.** `logprobs.content` has `completion_tokens − 1` entries
   (`inference_engine.cpp` argmax seed pick). Emit it.
4. **Stale model pin.** `bench/scripts/reference.lock` sha256 no longer matches the HF upload; a
   fresh `run.sh --download` re-fetches forever. Update the lock and fail fast instead of looping.
5. **Timing fields location.** Currently under `usage`; either is fine per R5, just document it.
6. **Container image / Dockerfile.** We build `entrius/sparkinfer:<commit>` ourselves
   (`docker/sparkinfer.Dockerfile`); an upstream Dockerfile would keep the two from drifting.

## Changelog

- **2026-08-22** — §4.2 planted-cheater experiment; verifier moves to a rolling-window aggregate.
- **v0 (2026-08-21)** — initial contract extracted from `feat/serving-scaffold` step 0. Same day: added R8
  (teacher-forced scoring) and §4.1 (validator-owned live reference; bank demoted to fallback).
