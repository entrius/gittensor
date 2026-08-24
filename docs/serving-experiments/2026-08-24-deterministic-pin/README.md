# Deterministic pin verification — 2026-08-24

**Question.** sparkinfer#910 (`9e43bfa`) claims bit-reproducible greedy decode
(`SPARKINFER_DETERMINISTIC=1`), a `/v1/score` endpoint and the first-token logprob. Does that hold
on *our* image, and what does it do to the verifier?

**Answer.** It holds exactly. An honest miner reproduces the reference's tokens and logprobs with
zero deviation on 40/40 prompts; every planted cheater deviates on 40/40 prompts. The verifier
therefore moved from "average positional overlap over 20 audits" back to a **decisive per-audit
verdict** (all tokens match, mean |Δlogprob| ≤ 0.005, max |Δlogprob| ≤ 0.10), with the window kept
only to absorb transient misses (`SERVING_AUDIT_WINDOW_THRESHOLDS = ((1, 0.85),)`). This
supersedes [`../2026-08-22-planted-cheater`](../2026-08-22-planted-cheater/README.md).

Two new upstream defects were found while doing it (see *Defects* below); one of them matters for
miner availability.

## Setup

| | |
|---|---|
| hardware | 1× RTX 5090 rented on Lium (`cosmic-fox-6e`, $0.67/h, ~1.4 h, $0.92) |
| reference | our image `entrius/sparkinfer:9e43bfa` (docker/sparkinfer.Dockerfile, `SPARKINFER_DETERMINISTIC=1` baked in), Qwen3.6-35B-A3B UD-Q4_K_M sha `ac0e2c11…` |
| prompts | 40 × `make_prompts(40, seed=4242)`, 64 tokens, temperature 0, logprobs on — same seed as 08-22 but the reference was **re-recorded** (the pin's Q4_K requant refinement changes the numbers) |
| candidates | honest re-run (same server); llama.cpp `server-cuda` with the honest Q4_K_M weights; llama.cpp with UD-Q2_K_XL — both `--jinja --chat-template-kwargs '{"enable_thinking":false}'` |

Each candidate's outputs were saved (`candidate` field on every row) and afterwards **teacher-force
scored** under the reference (`serving_cheat_experiment.py tfscore` → `POST /v1/score` with the
candidate's completion text): `tf_mean_abs_diff` / `tf_max_abs_diff` compare the logprobs the
candidate *reported* with what the reference assigns those same tokens; `tf_argmax_agreement` is
the fraction of positions where the candidate's token is the reference's own top-1.

## Conformance (`scripts/check_serving_runtime.py`, 30 prompts × 3 repeats, 16 parallel)

All MUSTs pass, including the new R8 checks, with one benign exception (EOS, below):

```
[PASS] R2 entries == completion_tokens    52 vs 52          <- first-token logprob fixed
[PASS] R8 POST /v1/score                  40 tokens
[FAIL] R8 scored tokens == generated tokens 40 vs 41       <- generation lists <|im_end|>, the text can't
[PASS] D1 prefix agreement p05 >= 0.99    min 1.000 p05 1.000 median 1.000
[PASS] D1 logprob drift p95 <= 0.01       max 0.0000 p95 0.0000 median 0.0000
[PASS] R6 429 under overload (no queue)   16 parallel: 16x200 ... (see Defects: sometimes it crashes instead)
```

TTFT 26 ms, 384 tok/s decode — unchanged from `1b8b962`.

## Results

```
candidate                prefix med   overlap med   mean|Δ| med/max   tf mean|Δ| min/med   tf max|Δ| min/med   tf argmax min   ms
honest re-run            1.000        1.000         0.0000 / 0.0000   0.0000 / 0.0000      0.0000 / 0.0000     1.000           160
llama.cpp, Q4_K_M        0.245        0.258         0.0443 / 0.1450   0.0057 / 0.0673      0.1290 / 0.5847     0.800           449
llama.cpp, Q2_K_XL       0.125        0.188         0.0707 / 0.2550   0.0222 / 0.2019      0.5881 / 1.6150     0.741           436
```

Per-audit pass rate under the shipped bands (prefix 1.0, mean ≤ 0.005, max ≤ 0.10): honest **1.000**,
llama/Q4_K_M **0.000**, llama/Q2_K_XL **0.000**. The margins: honest sits at exactly 0; the nearest
cheater prompt has mean 0.0057 *and* max 0.129, and both bands must hold. Looser pairs
(0.01 / 0.25) let 2.5 % of llama/Q4_K_M audits through; tighter (0.002 / 0.05) changes nothing for
honest — the bands are set with room for driver/firmware variance between 5090s, which this run
could not measure (one GPU).

Observations:

1. Determinism is real and batch-invariant on our image: 90 repeats, zero forks, zero drift.
2. With an exact reference, `/v1/score` is not needed *per audit* — a cheater that emits different
   tokens already fails, and one that copies the right tokens still cannot fabricate the exact
   logprobs (the `tf_*` columns show the same separation). It is the primitive for scoring
   non-greedy organic traffic, which is why `LiveReference.score` exists.
3. Cheater prompts occasionally match the reference's tokens perfectly (positional overlap max 1.0)
   — that is why the verdict includes the logprob bands, not tokens alone.
4. `tf_tokenization_mismatch` is set on 12–14 rows per candidate. For the honest run it is purely
   the trailing `<|im_end|>` (generation's `logprobs.content` lists it, the text cannot); for
   llama.cpp it is also re-tokenisation of the text. Metrics are computed over the aligned prefix.
5. llama.cpp is ~2.8× slower on the same GPU (449 vs 160 ms) — latency credit sees it.

## Defects found upstream (not ours, reported)

1. **Burst crash.** 16 concurrent requests with `logprobs` sometimes kill the server:
   `[kv] copy block table: operation would make the legacy stream depend on a capturing blocking
   stream` → `[qwen35] end capture: operation failed due to a previous error during capture` →
   `terminate called after throwing an instance of 'std::length_error'` or `corrupted size vs.
   prev_size` (exit 139). Seen with `DETERMINISTIC=1` (3 of ~7 sixteen-bursts) **and** `=0`
   (3 of 3 in one series, 0 of 5 in another); never at ≤ 12 concurrent. A CUDA-graph capture race
   under continuous batching. `1b8b962` survived the same probe once on 08-21. Not a verifier issue;
   a miner availability issue — a hostile client can take a miner down. Until fixed, cap concurrency
   at the miner's edge.
2. **EOS in `logprobs.content`.** Generation reports the `<|im_end|>` token as the last logprob
   entry; OpenAI does not list EOS. Harmless for us (both sides are sparkinfer), but any consumer
   that zips `logprobs.content` with the text is off by one at the end.
3. Some 16-bursts also return a few `400`s (2 of 16) — body not captured; likely the same capture
   failure surfacing before the crash.

## Files

| file | what |
|---|---|
| `reference.json` | 40 prompts with the `9e43bfa` reference's tokens/logprobs/completions. Re-score any candidate on this pin against it without a new recording. |
| `honest-rerun.json` | per-prompt rows + candidate outputs + `tf_*` metrics (the calibration set; every value is 0/1) |
| `cheat-q2kxl-llamacpp.json`, `honestweights-llamacpp.json` | same for the cheaters |

`tests/validator/test_serving.py::test_audit_bands_calibrated_on_deterministic_pin_data` replays
these rows through `verify_response` — honest must pass 40/40, each cheater must fail 40/40.

## Reproduce

```bash
# pod: build docker/sparkinfer.Dockerfile with SPARKINFER_REF=9e43bfa..., run with MODEL_SHA256=ac0e2c11...
python scripts/check_serving_runtime.py --model-id qwen3.6-35b-a3b --determinism-count 30 --repeat 3 --parallel 16
python scripts/serving_cheat_experiment.py record  --model-id qwen3.6-35b-a3b --count 40 --seed 4242 --out reference.json
python scripts/serving_cheat_experiment.py score   --model-id qwen3.6-35b-a3b --ref reference.json --label honest --out honest.json
# swap to a candidate server, `score` it the same way, then bring the reference back and
python scripts/serving_cheat_experiment.py tfscore --inp <candidate>.json --ref reference.json --model-id qwen3.6-35b-a3b --out <candidate>_tf.json
```

## When to redo this

- Any new runtime pin (this is the acceptance test for blessing one).
- Before adding a second GPU model: upstream promises reproducibility per GPU model only.
- With two *different* 5090 hosts (driver/firmware variance) to confirm the 0.005 / 0.10 bands
  have the margin we assume — this run had one GPU.
