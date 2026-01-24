# syntax=docker/dockerfile:1.4
FROM python:3.12-slim-bookworm

# Install system dependencies
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    build-essential curl git \
 && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install --break-system-packages uv

# Create non-root user 
RUN useradd -m -u 1000 gittensor

WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Create venv and sync dependencies
ENV VENV_DIR=/opt/venv
ENV VIRTUAL_ENV=$VENV_DIR
ENV PATH="$VENV_DIR/bin:$PATH"
RUN uv venv --python python3 $VENV_DIR && uv sync

# Copy application code and install
COPY . .
RUN uv pip install -e .

# Set ownership and switch to non-root user
RUN chown -R gittensor:gittensor /app /opt/venv
USER gittensor
