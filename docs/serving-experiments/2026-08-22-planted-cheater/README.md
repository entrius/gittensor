# Planted-cheater experiment — 2026-08-22

**Question.** sparkinfer `1b8b962` is not logit-reproducible run to run (contract §4, D1). Can the
validator still tell an honest miner from a cheaper one, and how many audits does it take?

**Answer.** Not per audit — an honest miner passes the original per-audit bands only 37 % of the
time. Yes over a window: the mean *positional overlap* of the last 10–20 audits separates every
cheater we planted at a 1 % honest false-positive rate. This is why `gittensor/serving/audit.py`
scores with `AuditWindow` rather than a per-audit verdict, and where
`SERVING_AUDIT_OVERLAP_THRESHOLDS` in `gittensor/constants.py` comes from.

## Setup

| | |
|---|---|
| hardware | 1× RTX 5090 rented on Lium (`swift-comet-bb`, $0.60/h, ~1.3 h total) |
| reference | sparkinfer `gittensor-ai-lab/sparkinfer@1b8b962`, our image `docker/sparkinfer.Dockerfile`, `unsloth/Qwen3.6-35B-A3B-GGUF` UD-Q4_K_M (sha `ac0e2c11…`), DFlash off |
| prompts | 40 × `make_prompts(40, seed=4242)` from `gittensor/serving/probe.py`, 64 tokens, temperature 0, `logprobs=true, top_logprobs=1` |
| candidates | see table — each served the same 40 prompts; one candidate at a time on the same GPU |

Candidates:

- **honest re-run** — the reference server queried again (same process).
- **honest under load** — same, while 8 background loops hammered it with 256-token, temp-0.7 requests.
- **Q4_K_S / sparkinfer** — the only cheaper quant sparkinfer can load (its loader accepts F32/F16/Q8_0/Q4_K/Q5_K/Q6_K tensors only; Q2/Q3/IQ fail with `unsupported ggml type`).
- **honest weights / llama.cpp** — `ghcr.io/ggml-org/llama.cpp:server-cuda` on the same Q4_K_M file. Isolates *runtime* substitution.
- **Q2_K_XL / llama.cpp** — what a lazy cheater would actually run.

llama.cpp was started with `--jinja --chat-template-kwargs '{"enable_thinking":false}'` to match
sparkinfer's non-thinking output, and scored with `--drop-first` because sparkinfer omits the first
token's logprob (contract §9 #3) — without it every cross-runtime comparison is shifted by one.

## Metrics (per prompt)

- `prefix_agreement` — fraction of the reference's tokens reproduced before the first divergence (what the original `verify_response` used).
- `mean_abs_logprob_diff` — mean |Δlogprob| over that agreed prefix.
- `positional_overlap` — fraction of positions whose token matches the reference, ignoring divergence. Ranks candidates the same as prefix agreement but wastes fewer tokens, so it's the window metric.
- `ms` — wall-clock per request from the pod.

## Results

```
candidate                                prefix med overlap med drift med drift max ms med
honest-rerun                                  0.516       0.563     0.016     0.195    163
honest-under-load8                            0.468       0.651     0.016     0.179    163
cheat-sparkinfer-Q4_K_S (summary only)        0.333       0.397     0.033     0.108    161
llamacpp-honest-Q4_K_M                        0.198       0.238     0.045     0.154    604
cheat-llamacpp-Q2_K_XL                        0.087       0.114     0.061     0.233    566

metric = mean positional_overlap over the last k audits; threshold at 1% honest false positives
  k threshold cheat-llamacpp-Q2_K_XL llamacpp-honest-Q4_K_M
  1     0.016                  0.101                  0.000
  3     0.143                  0.480                  0.094
  5     0.232                  0.827                  0.269
 10     0.339                  0.998                  0.608
 20     0.415                  1.000                  0.928
 40     0.470                  1.000                  0.999
```

The threshold column is the `(k, threshold)` table in `SERVING_AUDIT_OVERLAP_THRESHOLDS`. The
power columns are the probability a cheater's k-audit mean falls below that threshold (bootstrap,
20 000 draws, per-prompt rows resampled with replacement — so this treats audits as independent
draws from the 40-prompt distribution).

Observations:

1. Per-audit pass/fail is unusable against this pin: honest p05 of prefix agreement is 0.02.
2. Batching does not change the honest distribution (rows 1–2), so audits can run during live traffic.
3. Logprob drift on the agreed prefix is a weak separator here (0.016 vs 0.061) because the prefix is short whenever the models disagree. It becomes the primary signal only with teacher-forced scoring (contract R8).
4. Runtime substitution alone is visible (0.24 vs 0.56) and ~3.7× slower.
5. Q4_K_S under sparkinfer is the hardest case (0.40 vs 0.56); with the per-prompt rows lost we can't bootstrap it, but by eye it needs k ≳ 40. A 5 % smaller file is also the least rewarding cheat.

## What else the data calibrates

- **Latency credit** (`SERVING_LATENCY_FULL_CREDIT_MS = 500`, `ZERO = 1500`, `gittensor/constants.py`):
  honest on-box p95 is 166 ms, so honest + any earthly RTT stays under the full-credit line, while a
  miner proxying audits to a GPU in another region (one more RTT each way) or to a slower runtime
  (llama.cpp ≈ 600 ms) loses credit in proportion. Latency cannot see a proxy in the *same* region
  — that is the one-GPU-many-hotkeys case, which concurrent burst audits address.
  `test_latency_credit_calibrated_on_experiment_data` replays the `ms` column.

## Files

| file | what |
|---|---|
| `reference.json` | the 40 prompts with the reference's tokens/logprobs/completions. **Re-score any new candidate against this without re-recording** (same release only). |
| `honest-rerun.json`, `honest-under-load.json` | per-prompt rows for the two honest runs (the calibration set) |
| `cheat-q2kxl-llamacpp.json`, `honestweights-llamacpp.json` | per-prompt rows for the two llama.cpp candidates |
| `cheat-q4ks-sparkinfer.summary.json` | summary only — the rows were lost (`lium scp` failed silently before the pod was removed; verify the copy before `lium rm`) |

## Reproduce

```bash
# on a 5090 with the reference release running on :8080 (see docker/sparkinfer.Dockerfile)
python scripts/serving_cheat_experiment.py record --model-id qwen3.6-35b-a3b --count 40 --seed 4242 --out reference.json
python scripts/serving_cheat_experiment.py score  --model-id qwen3.6-35b-a3b --ref reference.json --label honest-rerun --out honest-rerun.json
# swap the server to a candidate (MODEL_FILE + MODEL_SHA256 env for sparkinfer; llama.cpp as above), then
python scripts/serving_cheat_experiment.py score  --model-id qwen3.6-35b-a3b --ref reference.json [--drop-first] --label <name> --out <name>.json
# offline, any time:
python scripts/serving_cheat_experiment.py analyze --honest honest-rerun.json honest-under-load.json --cheaters cheat-q2kxl-llamacpp.json honestweights-llamacpp.json
```

`tests/validator/test_serving.py::test_audit_window_calibrated_on_experiment_data` replays these
rows through `AuditWindow` so the thresholds can't silently drift from the data.

## When to redo this

- A new runtime pin (especially one that fixes D1 — the thresholds should tighten a lot).
- A new release/model: record a fresh `reference.json` and at least one honest re-run; the
  thresholds are per-release in principle even though v0 ships one table.
- When R8 (teacher-forced scoring) lands: score every position, expect drift to become the primary metric.
