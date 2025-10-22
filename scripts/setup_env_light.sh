#!/bin/bash

# Exit on error
set -e

# Get the absolute path of the project root
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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