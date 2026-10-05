# syntax=docker/dockerfile:1.4
# Containerized conformance checker: scripts/check_serving_runtime.py without cloning the repo. CPU-only —
# it talks to the runtime and attest containers over HTTP, so --network host is the documented run mode:
#
#   docker run --rm --network host entrius/gt-checker \
#     --base-url http://127.0.0.1:8080 --model-id <model id> --attest-url http://127.0.0.1:8081
#
# Built FROM the published neuron image (.github/workflows/docker-publish.yml) so the checker always matches
# the repo revision it shipped with.
ARG BASE_IMAGE=entrius/gittensor:latest
FROM ${BASE_IMAGE}
ENTRYPOINT ["python", "scripts/check_serving_runtime.py"]
