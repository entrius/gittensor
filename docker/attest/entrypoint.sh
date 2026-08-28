#!/usr/bin/env bash
# Start the attestation sidecar beside sparkinfer_server, then hand over to the runtime's own entrypoint.
python3 /opt/sparkinfer/bin/attest_server.py &
exec bash server/run.sh --download "$@"
