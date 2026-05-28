# syntax=docker/dockerfile:1.4

# ---- builder: toolchain + uv resolve the locked deps into /app/.venv ----
FROM python:3.12-slim-bookworm AS builder

# Pinned official uv binary (reproducible; no unpinned pip install).
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /uvx /bin/

# build-essential is only needed to compile any sdist-only wheels during sync;
# it stays in this stage and never reaches the runtime image. (No git+ deps in
# uv.lock, so git isn't needed here either.)
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

# Dependency layer first for caching. --frozen pins the install to the committed
# uv.lock and fails the build if pyproject.toml / uv.lock have drifted.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project

# Copy application code and install the project.
COPY . .
RUN uv sync --frozen

# ---- runtime: slim image with no build toolchain ----
FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

# Bring over the resolved venv + application; the venv's interpreter paths match
# because both stages share the same base image.
COPY --from=builder /app /app
