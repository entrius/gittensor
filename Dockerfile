# Gittensor Validator Docker Image
# Multi-stage build for smaller final image

FROM python:3.12-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster dependency installation
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /build

# Copy all source files needed for build
COPY requirements.txt pyproject.toml setup.py README.md ./
COPY gittensor/ gittensor/
COPY neurons/ neurons/

# Create virtual environment and install dependencies
RUN uv venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN uv pip install --no-cache -r requirements.txt
RUN uv pip install --no-cache .

# Production image
FROM python:3.12-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user with UID 1000 for wallet mount compatibility
RUN groupadd -g 1000 validator && \
    useradd -u 1000 -g validator -m -s /bin/bash validator

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Set working directory
WORKDIR /app

# Copy application code (package is installed in venv, but we need neurons for execution)
COPY --chown=validator:validator neurons/ neurons/

# Copy and set up entrypoint
COPY --chown=validator:validator docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Create directories for wallet mount and set permissions
RUN mkdir -p /home/validator/.bittensor/wallets && \
    chown -R validator:validator /home/validator/.bittensor

# Switch to non-root user
USER validator

# Default port for validator axon
EXPOSE 8099

# Health check - verify Python can import the module
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import gittensor; print('healthy')" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["validator"]
