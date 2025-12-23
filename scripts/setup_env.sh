#!/bin/bash

# Exit on error
set -e

# Get the absolute path of the project root
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# System Updates and Package Installation

# Update system
sudo apt update -y

# Install core dependencies
sudo apt install -y \
    python3-pip \
    python3-venv \
    curl \
    ca-certificates

# Node.js / npm setup
# NOTE: Installing `npm` via apt can fail on Ubuntu 24.04 due to dependency conflicts.
# NodeSource Node.js packages include npm, so we install Node.js instead (only if needed).
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt install -y nodejs
fi

# Install process manager if not already installed
if ! command -v pm2 &> /dev/null; then
    sudo npm install -g pm2@latest
fi

# Virtual Environment Setup

# Set default virtual environment path if not specified in .env
VENV_PATH=${VENV_PATH:-"$PROJECT_ROOT/gittensor-venv"}

# Create virtual environment if it doesn't exist
if [ ! -d "$VENV_PATH" ]; then
    echo "Creating virtual environment at $VENV_PATH"
    python3 -m venv "$VENV_PATH"
fi

# Activate virtual environment
echo "Activating virtual environment..."
source "$VENV_PATH/bin/activate"


# Python Package Installation

# Change to project root directory
cd "$PROJECT_ROOT"

# Install project dependencies
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .

echo "Environment setup completed successfully."