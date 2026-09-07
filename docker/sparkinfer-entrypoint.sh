#!/usr/bin/env bash
# Entrypoint of entrius/sparkinfer:<ref>: fetch the blessed model artifact, verify it against the loadout's digests,
# start sparkinfer_server on it.
#
# Two artifact shapes, picked by env (docker-compose.miner.yml / the validator's reference profile pass them from the
# release card):
#   * one GGUF file (the default, e.g. Qwen3.6-35B-A3B UD-Q4_K_M): MODEL_REPO / MODEL_FILE / TOK_REPO / MODEL_NAME /
#     MODEL_SHA256 as upstream's server/run.sh reads them; run.sh --download fetches and verifies the file.
#   * a Hugging Face model DIRECTORY (compressed-tensors NVFP4/FP8 safetensors, e.g. Qwen3.8-27B-NVFP4-RTX5090):
#     MODEL_DIR_REPO=<hf repo id>, MODEL_DIR_REVISION=<hf commit sha> (the repo is mutable; the revision is the pin),
#     MODEL_DIR_SHA256="<file>=<sha256>[,<file>=<sha256>...]" for every weight shard the release lists. The directory is
#     fetched at that revision into $MODELS_DIR/<repo name>, every listed file's sha256 is checked (a mismatch refuses to
#     start, never re-downloads over it), then run.sh serves the directory: sparkinfer_server dispatches on `-m <dir>`.
set -euo pipefail
MODELS_DIR="${MODELS_DIR:-/opt/sparkinfer/models}"

if [ -z "${MODEL_DIR_REPO:-}" ]; then
  exec bash server/run.sh --download "$@"
fi

: "${MODEL_DIR_REVISION:?MODEL_DIR_REVISION (the Hugging Face commit sha of $MODEL_DIR_REPO) is the model pin; refusing to serve an unpinned directory}"
: "${MODEL_DIR_SHA256:?MODEL_DIR_SHA256 (file=sha256,...) for the weight shards of the release is required}"
DIR="$MODELS_DIR/$(basename "$MODEL_DIR_REPO")"
mkdir -p "$MODELS_DIR"
if [ ! -f "$DIR/.revision" ] || [ "$(cat "$DIR/.revision")" != "$MODEL_DIR_REVISION" ]; then
  echo ">> downloading $MODEL_DIR_REPO@$MODEL_DIR_REVISION into $DIR ..." >&2
  HF_HUB_DISABLE_XET=1 hf download "$MODEL_DIR_REPO" --revision "$MODEL_DIR_REVISION" --local-dir "$DIR" \
    --exclude 'assets/*' >&2
  printf '%s\n' "$MODEL_DIR_REVISION" > "$DIR/.revision"
fi
IFS=',' read -r -a pins <<< "$MODEL_DIR_SHA256"
for pin in "${pins[@]}"; do
  file="${pin%%=*}"; want="${pin#*=}"
  got="$(sha256sum "$DIR/$file" 2>/dev/null | awk '{print $1}' || true)"
  if [ "$got" != "$want" ]; then
    echo ">> FATAL: $file sha256 MISMATCH (got ${got:-<missing>}, want $want). Not the pinned build; refusing to serve." >&2
    exit 1
  fi
  echo ">> $file sha256 OK" >&2
done
# The directory carries its own tokenizer; run.sh still wants one under MODELS_DIR for --tokenizer.
[ -f "$MODELS_DIR/tokenizer.json" ] || cp "$DIR/tokenizer.json" "$MODELS_DIR/tokenizer.json"
export TOK_REPO="${TOK_REPO:-$MODEL_DIR_REPO}"
exec bash server/run.sh "$DIR" "$@"
