#!/usr/bin/env bash
# Conformance-check a built serving runtime image on a rented RTX 5090 (Lium), then tear the pod down.
#
#   LIUM_API_KEY=... scripts/serving_conformance_on_lium.sh <sparkinfer ref> [out dir]
#
# Rents one 1x5090 executor, boots entrius/sparkinfer:<ref> with the loadout's MODEL_SHA256, waits for the
# model download, runs scripts/check_serving_runtime.py against the pod's public 8080 and writes the report
# to <out dir>/conformance.txt and the blessing-time speed profile to <out dir>/speed.json. Exit code is the checker's (1 = a MUST failed). The pod is removed on every
# exit path; --ttl is the backstop if this process dies.
set -euo pipefail

REF="${1:?sparkinfer ref (image tag)}"
OUT="${2:-serving-conformance-$REF}"
LOADOUT="$(dirname "$0")/../gittensor/validator/weights/serving_loadout.json"
MODEL_ID=$(jq -r '.releases[0].model_id' "$LOADOUT")
MODEL_SHA=$(jq -r '.releases[0].model_sha256' "$LOADOUT")
POD="conf-${REF:0:7}-$RANDOM"
GPU="${CONFORMANCE_GPU:-5090}"
RENT_WAIT_MIN="${CONFORMANCE_RENT_WAIT_MIN:-60}"
BOOT_WAIT_MIN="${CONFORMANCE_BOOT_WAIT_MIN:-45}"
mkdir -p "$OUT"

cleanup() { lium rm "$POD" -y >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "renting 1x$GPU for entrius/sparkinfer:$REF (waiting up to ${RENT_WAIT_MIN} min for a free card)"
for ((i = 0; i < RENT_WAIT_MIN; i++)); do
  # `lium ls --gpu X --format json` returns [] even when cards are listed; filter the full listing instead.
  NODE=$(lium ls --format json 2>/dev/null | jq -r --arg gpu "RTX$GPU" '[.[] | select(.gpu_type == $gpu and .gpu_count == 1 and .vram_gb >= 30 and .download_mbps >= 150)] | sort_by(.price_per_hour) | .[0].id // empty')
  [ -n "$NODE" ] && break
  sleep 60
done
[ -n "${NODE:-}" ] || { echo "no 1x$GPU executor became available in ${RENT_WAIT_MIN} min"; exit 2; }
lium up "$NODE" --name "$POD" --image "entrius/sparkinfer:$REF" --internal-ports 22,8080 \
  -e "MODEL_SHA256=$MODEL_SHA" -e SPARKINFER_DETERMINISTIC=1 --ttl 3h -y --no-ssh

echo "waiting for the pod's public 8080 and the model download (up to ${BOOT_WAIT_MIN} min)"
BASE=""
for ((i = 0; i < BOOT_WAIT_MIN * 6; i++)); do
  if [ -z "$BASE" ]; then
    BASE=$(lium ps --format json 2>/dev/null | python3 -c '
import json, sys
name = sys.argv[1]
for p in json.load(sys.stdin):
    if p.get("name") != name or p.get("status") != "RUNNING":
        continue
    ports = p.get("ports") or {}
    items = ports.items() if isinstance(ports, dict) else [(x.get("internal") or x.get("internal_port"), x.get("external") or x.get("external_port")) for x in ports]
    for internal, external in items:
        if str(internal) == "8080" and external:
            print(f"http://{p.get(\"ssh_ip\") or p.get(\"ip\")}:{external}")
' "$POD" || true)
  fi
  if [ -n "$BASE" ] && curl -sf --max-time 5 "$BASE/v1/models" >/dev/null 2>&1; then break; fi
  sleep 10
done
[ -n "$BASE" ] && curl -sf --max-time 5 "$BASE/v1/models" >/dev/null || { echo "runtime never answered on $BASE"; lium ps; exit 2; }
echo "runtime up at $BASE"
curl -s "$BASE/v1/models" | tee "$OUT/models.json"; echo

set +e
uv run python scripts/check_serving_runtime.py --base-url "$BASE" --model-id "$MODEL_ID" \
  --determinism-count 30 --repeat 3 --parallel 16 --speed-json "$OUT/speed.json" 2>&1 | tee "$OUT/conformance.txt"
RC=${PIPESTATUS[0]}
set -e
echo "checker exit $RC (report: $OUT/conformance.txt)"
exit "$RC"
