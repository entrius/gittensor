# The cutover branch

`compute/cutover` is one commit: the retirement of the phase-0 serving path (the attest sidecar, teacher-forced
audits, per-token pay, the validator's built-in `:8790` gateway, the serving miner). The compute pool replaces it.
It is prepared ahead of time so cutover day is a mechanical merge. **It is not merged anywhere until cutover day**;
mainnet runs phase 0 until then.

## Keeping it current

The branch is stacked on `compute/pool-step1`. Rebase it whenever that branch moves:

```bash
git fetch origin
git checkout compute/cutover
git rebase origin/compute/pool-step1
uv run pytest tests -q && uv run ruff check
git push --force-with-lease origin compute/cutover
```

If `compute/pool-step1` has already been merged into `test`, rebase onto `origin/test` instead.

## Must be live before this commit merges

This commit only deletes and trims. Both things it depended on now exist on `compute/pool-step1`; on the day they
have to be *running*, not just merged:

- **The gateway container** (`gitt gateway`, `docker/gateway/`) in front of das, with das pointed at it
  (`VALIDATOR_GATEWAY_URL=http://gt-gateway:8790`, `GATEWAY_KEY` set; the das side is gittensor-app branch
  `compute/das-passthrough`). `serving/api.py`, the request path this commit deletes, is replaced by it.
- **The controller with the pay ledger** (`gitt controller run`) writing a scorecard, and the validator started with
  `COMPUTE_SCORECARD_PATH` pointing at it. This commit leaves the validator paying the compute share only from a
  valid scorecard; with the variable unset or the scorecard stale, that share recycles by design.

## Cutover day, in order

1. Rebase onto the current `compute/pool-step1` (or `test`), as above.
2. The branch's tests are green: `uv run pytest tests -q`, `uv run ruff check`.
3. Merge to `test`.
4. Soak.
5. Merge `test` to `main`.
