#!/usr/bin/env bash
# Write and sign the agent release channel. Run by CI after the images are pushed, with the release private key.
#
#   docker/agent/channel/sign.sh --key <gt_release private key> --agent sha256:<digest> --runner sha256:<digest> \
#       --version 5.1.0 [--out docker/agent/channel/stable.json]
set -euo pipefail
KEY='' AGENT='' RUNNER='' VERSION='' OUT="$(dirname "$0")/stable.json"
while [ $# -gt 0 ]; do
    case "$1" in
        --key) KEY="$2"; shift 2 ;;
        --agent) AGENT="$2"; shift 2 ;;
        --runner) RUNNER="$2"; shift 2 ;;
        --version) VERSION="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        *) echo "unknown argument $1" >&2; exit 2 ;;
    esac
done
[ -n "$KEY" ] && [ -n "$AGENT" ] && [ -n "$RUNNER" ] && [ -n "$VERSION" ] || { echo 'usage: --key --agent --runner --version [--out]' >&2; exit 2; }
for d in "$AGENT" "$RUNNER"; do
    [[ "$d" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "not a sha256 digest: $d" >&2; exit 2; }
done
printf '{\n  "agent": "entrius/gt-agent@%s",\n  "runner": "entrius/gt-agent-runner@%s",\n  "version": "%s",\n  "issued_at": %d\n}\n' \
    "$AGENT" "$RUNNER" "$VERSION" "$(date +%s)" > "$OUT"
rm -f "$OUT.sig"
ssh-keygen -Y sign -f "$KEY" -n gt-agent-channel "$OUT"
echo "wrote $OUT and $OUT.sig"
