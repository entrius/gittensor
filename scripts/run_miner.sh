#!/bin/bash

# Exit on error
set -e

# Get the absolute path of the project root
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# load env files from miner/.env, if not exists then quit
if [ -f "$PROJECT_ROOT/gittensor/miner/.env" ]; then
    echo "Loading environment variables from .env file..."
    export $(cat "$PROJECT_ROOT/gittensor/miner/.env" | grep -v '^#' | sed 's/#.*//' | xargs)
else
    echo "Error: .env file not found at $PROJECT_ROOT/gittensor/miner/.env"
    exit 1
fi

MINER_PROCESS_NAME="gt-miner"
VENV_PATH="$PROJECT_ROOT/gittensor-venv"

# Activate virtual environment
if [ ! -d "$VENV_PATH" ]; then
    echo "Error: Virtual environment not found at $VENV_PATH"
    echo "Please run setup_env.sh first!"
    exit 1
fi

echo "Activating virtual environment..."
source "$VENV_PATH/bin/activate"

# Ensure required environment variables are set
if [ -z "$NETUID" ] || [ -z "$WALLET_NAME" ] || [ -z "$HOTKEY_NAME" ]; then
    echo "Error: Required environment variables NETUID, WALLET_NAME, and HOTKEY_NAME must be set in .env file"
    exit 1
fi

# Default values for optional parameters
SUBTENSOR_NETWORK=${SUBTENSOR_NETWORK:-"finney"}
PORT=${PORT:-8098}
LOGGING=${LOGGING:-"--logging.info"}

if pm2 describe "$MINER_PROCESS_NAME" >/dev/null 2>&1; then
    echo "Process '$MINER_PROCESS_NAME' is already running. Killing and rerunning..."
    pm2 delete "$MINER_PROCESS_NAME" || pm2 delete "$MINER_PROCESS_NAME" --silent || true	
    GITTENSOR_MINER_PAT="$GITTENSOR_MINER_PAT" pm2 start python --name "$MINER_PROCESS_NAME" \
        -- neurons/miner.py \
        --netuid "$NETUID" \
        --subtensor.network "$SUBTENSOR_NETWORK" \
        --wallet.name "$WALLET_NAME" \
        --wallet.hotkey "$HOTKEY_NAME" \
        --axon.port "$PORT" \
        $LOGGING
else
    echo "Process '$MINER_PROCESS_NAME' is not running. Starting it for the first time..."
    GITTENSOR_MINER_PAT="$GITTENSOR_MINER_PAT" pm2 start python --name "$MINER_PROCESS_NAME" \
        -- neurons/miner.py \
        --netuid "$NETUID" \
        --subtensor.network "$SUBTENSOR_NETWORK" \
        --wallet.name "$WALLET_NAME" \
        --wallet.hotkey "$HOTKEY_NAME" \
        --axon.port "$PORT" \
        $LOGGING
fi
