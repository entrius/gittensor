#!/usr/bin/env bash
# Conformance-check a built serving runtime image on a rented RTX 5090 (Lium), then tear the pod down.
#
#   LIUM_API_KEY=... scripts/serving_conformance_on_lium.sh <sparkinfer ref> [out dir]
#
# Rents two 1x5090 executors: one boots entrius/sparkinfer:<ref> with the loadout's MODEL_SHA256, the other the
# attest container (attest.image in the loadout — a Lium pod runs one image, so the sidecar gets its own card).
# Waits for the model download, runs scripts/check_serving_runtime.py against the runtime pod's public 8080 with
# --attest-url at the attest pod's 8081, and writes the report to <out dir>/conformance.txt and the blessing-time
# speed profile to <out dir>/speed.json. Exit code is the checker's (1 = a MUST failed). Both pods are removed on
# every exit path; --ttl is the backstop if this process dies.
set -euo pipefail

REF="${1:?sparkinfer ref (image tag)}"
OUT="${2:-serving-conformance-$REF}"
LOADOUT="$(dirname "$0")/../gittensor/validator/weights/serving_loadout.json"
MODEL_ID=$(jq -r '.releases[0].model_id' "$LOADOUT")
MODEL_SHA=$(jq -r '.releases[0].model_sha256' "$LOADOUT")
ATTEST_IMAGE="${CONFORMANCE_ATTEST_IMAGE:-$(jq -r '.releases[0].attest.image' "$LOADOUT")}"
POD="conf-${REF:0:7}-$RANDOM"
ATTEST_POD="$POD-attest"
GPU="${CONFORMANCE_GPU:-5090}"
RENT_WAIT_MIN="${CONFORMANCE_RENT_WAIT_MIN:-60}"
BOOT_WAIT_MIN="${CONFORMANCE_BOOT_WAIT_MIN:-45}"
mkdir -p "$OUT"

cleanup() { lium rm "$POD" -y >/dev/null 2>&1 || true; lium rm "$ATTEST_POD" -y >/dev/null 2>&1 || true; }
trap cleanup EXIT

# `lium ls --gpu X --format json` returns [] even when cards are listed; filter the full listing instead.
# The element is bound to $n before the `taken` test: inside jq's contains(), `.` is the string being searched, so
# a bare .id there indexes a string and kills the run — and only ever when a matching card IS listed.
# Prints the cheapest free 1x$GPU executor id not in $1 (space-separated ids already taken).
free_node() {
  local listing
  listing=$(lium ls --format json 2>/dev/null || true)
  if ! printf '%s\n' "$listing" | jq -e 'type == "array"' >/dev/null 2>&1; then
    echo "lium ls returned no JSON listing this minute, retrying: ${listing:0:120}" >&2
    return 0
  fi
  printf '%s\n' "$listing" | jq -r --arg gpu "RTX$GPU" --arg taken " $1 " \
    '[.[] | select(type == "object") | . as $n
       | select(($n.id | type) == "string" and $n.gpu_type == $gpu and $n.gpu_count == 1
                and ($n.vram_gb // 0) >= 30 and ($n.download_mbps // 0) >= 150
                and ($taken | contains(" " + $n.id + " ") | not))]
     | sort_by(.price_per_hour // 1e9) | .[0].id // empty'
}

echo "renting 2x 1x$GPU: entrius/sparkinfer:$REF + $ATTEST_IMAGE (waiting up to ${RENT_WAIT_MIN} min for free cards)"
for ((i = 0; i < RENT_WAIT_MIN; i++)); do
  NODE=$(free_node "")
  ATTEST_NODE=$([ -n "$NODE" ] && free_node "$NODE" || true)
  [ -n "$NODE" ] && [ -n "$ATTEST_NODE" ] && break
  sleep 60
done
[ -n "${NODE:-}" ] && [ -n "${ATTEST_NODE:-}" ] || { echo "two 1x$GPU executors did not become available in ${RENT_WAIT_MIN} min"; exit 2; }
lium up "$NODE" --name "$POD" --image "entrius/sparkinfer:$REF" --internal-ports 22,8080 \
  -e "MODEL_SHA256=$MODEL_SHA" -e SPARKINFER_DETERMINISTIC=1 --ttl 3h -y --no-ssh
lium up "$ATTEST_NODE" --name "$ATTEST_POD" --image "$ATTEST_IMAGE" --internal-ports 22,8081 --ttl 3h -y --no-ssh

# public URL of a pod's internal port, empty until the pod is RUNNING with the port mapped
pod_url() {
  lium ps --format json 2>/dev/null | python3 -c '
import json, sys
name, port = sys.argv[1], sys.argv[2]
for p in json.load(sys.stdin):
    if p.get("name") != name or p.get("status") != "RUNNING":
        continue
    ports = p.get("ports") or {}
    items = ports.items() if isinstance(ports, dict) else [(x.get("internal") or x.get("internal_port"), x.get("external") or x.get("external_port")) for x in ports]
    for internal, external in items:
        if str(internal) == port and external:
            # No backslashes or nested double quotes in the f-string: this program is single-quoted into
            # python3 -c, and a backslash inside an f-string expression is a SyntaxError before 3.12.
            host = p.get("ssh_ip") or p.get("ip")
            print(f"http://{host}:{external}")
' "$1" "$2" || true
}

echo "waiting for the pods' public ports and the model download (up to ${BOOT_WAIT_MIN} min)"
BASE=""; ATTEST=""
for ((i = 0; i < BOOT_WAIT_MIN * 6; i++)); do
  [ -z "$BASE" ] && BASE=$(pod_url "$POD" 8080)
  [ -z "$ATTEST" ] && ATTEST=$(pod_url "$ATTEST_POD" 8081)
  if [ -n "$BASE" ] && [ -n "$ATTEST" ] && curl -sf --max-time 5 "$BASE/v1/models" >/dev/null 2>&1 \
     && curl -sf --max-time 5 "$ATTEST/info" >/dev/null 2>&1; then break; fi
  sleep 10
done
[ -n "$BASE" ] && curl -sf --max-time 5 "$BASE/v1/models" >/dev/null || { echo "runtime never answered on $BASE"; lium ps; exit 2; }
[ -n "$ATTEST" ] && curl -sf --max-time 5 "$ATTEST/info" >/dev/null || { echo "attest container never answered on $ATTEST"; lium ps; exit 2; }
echo "runtime up at $BASE, attest at $ATTEST"
curl -s "$ATTEST/info" | tee "$OUT/attest-info.json"; echo
curl -s "$BASE/v1/models" | tee "$OUT/models.json"; echo

set +e
uv run python scripts/check_serving_runtime.py --base-url "$BASE" --model-id "$MODEL_ID" --attest-url "$ATTEST" \
  --determinism-count 30 --repeat 3 --parallel 16 --speed-json "$OUT/speed.json" 2>&1 | tee "$OUT/conformance.txt"
RC=${PIPESTATUS[0]}
set -e
echo "checker exit $RC (report: $OUT/conformance.txt)"
exit "$RC"
