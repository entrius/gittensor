# syntax=docker/dockerfile:1.4
# The base challenge evaluator: gittensor's `gitt challenge eval` plus bubblewrap, the sandbox its runner needs.
# Published as entrius/gt-challenge-evaluator (.github/workflows/challenge-evaluator-image.yml). Each challenge's image
# is this plus its package at a pinned version, built by that challenge repo's release CI:
#
#   FROM entrius/gt-challenge-evaluator@sha256:<digest>
#   USER root
#   RUN uv pip install --python /app/.venv/bin/python "gt-challenge-intents @ git+https://github.com/entrius/gt-challenge-intents@v0.1.0"
#   USER evaluator
#
#   docker run --rm --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
#     --security-opt systempaths=unconfined -v "$PWD:/solvers" <image> gt_challenge_intents /solvers/challenger \
#     --king /solvers/king --seed-block-hash <hash> --json /solvers/result.json
#
# bwrap makes unprivileged user namespaces, which Docker's default seccomp and AppArmor profiles refuse, and mounts a
# fresh /proc, which Docker's masked /proc paths block: hence the three --security-opt flags (a narrower seccomp
# profile that allows unshare and namespaced clone works too). Without them `gitt challenge eval` refuses to run ("no
# sandbox here"). The evaluator runs as a non-root user, so RLIMIT_NPROC binds the solver.
# TODO: attest the result JSON under Polaris.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    build-essential curl git bubblewrap \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --break-system-packages uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
ENV PATH="/app/.venv/bin:$PATH"
RUN uv sync --no-install-project

COPY . .
RUN uv sync

RUN useradd --create-home evaluator
USER evaluator
ENTRYPOINT ["gitt", "challenge", "eval"]
