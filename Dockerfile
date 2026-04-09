# syntax=docker/dockerfile:1.4
FROM python:3.12-slim-bookworm

# Install system dependencies
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    build-essential curl git \
 && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install --break-system-packages uv

WORKDIR /app

# Copy dependency files first (for Docker layer caching)
COPY pyproject.toml uv.lock README.md ./

# Install dependencies only (no project install yet — source code not copied)
ENV PATH="/app/.venv/bin:$PATH"
RUN uv sync --no-install-project

# Copy application code and install the project
COPY . .
RUN uv sync
