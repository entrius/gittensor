#!/bin/bash

# Exit on error
set -e

# Get the absolute path of the project root
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# load env files from validator/.env, if not exists then quit
if [ -f "$PROJECT_ROOT/gittensor/validator/.env" ]; then
    echo "Loading environment variables from .env file..."
    # source "$PROJECT_ROOT/gittensor/validator/.env"
    export $(cat "$PROJECT_ROOT/gittensor/validator/.env" | grep -v '^#' | sed 's/#.*//' | xargs)
else
    echo "Error: .env file not found at $PROJECT_ROOT/gittensor/validator/.env"
    exit 1
fi

VENV_PATH="$PROJECT_ROOT/gittensor-venv"
VALIDATOR_PROCESS_NAME="gt-vali"

# Activate virtual environment
if [ ! -d "$VENV_PATH" ]; then
    echo "Error: Virtual environment not found at $VENV_PATH"
    echo "Please run setup_env.sh first!"
    exit 1
fi

echo "Activating virtual environment..."
source "$VENV_PATH/bin/activate"

# Ensure required environment variables are set
if [ -z "$NETUID" ]; then
  echo "Error: NETUID is not set in the .env file."
  exit 1
fi
if [ -z "$WANDB_API_KEY" ]; then
  echo "Error: WANDB_API_KEY is not set in the .env file."
  exit 1
fi
if [ -z "$WALLET_NAME" ]; then
  echo "Error: WALLET_NAME is not set in the .env file."
  exit 1
fi
if [ -z "$HOTKEY_NAME" ]; then
  echo "Error: HOTKEY_NAME is not set in the .env file."
  exit 1
fi

# Set default values for validator parameters if not set in .env
SUBTENSOR_NETWORK=${SUBTENSOR_NETWORK:-"finney"}
# SUBTENSOR_CHAIN_ENDPOINT=${SUBTENSOR_CHAIN_ENDPOINT:-"wss://entrypoint-finney.opentensor.ai:443"}
PORT=${PORT:-8099}
LOGGING=${LOGGING:-"--logging.info"}

# Build optional flags based on .env variables
OPTIONAL_FLAGS=""

# Add database flag if STORE_DB_RESULTS is set to true
if [ "${STORE_DB_RESULTS}" = "true" ]; then
  OPTIONAL_FLAGS="$OPTIONAL_FLAGS --database.store_validation_results"
fi

# Set up debugpy command prefix and API if DEBUGPY_PORT is set
DEBUGPY_PREFIX=""
if [ -n "${DEBUGPY_PORT}" ]; then
  echo ""
  echo "⚠️  DEBUG MODE DETECTED (DEBUGPY_PORT=${DEBUGPY_PORT} in .env)"
  echo "To disable: unset DEBUGPY_PORT in gittensor/validator/.env"
  echo ""
  echo "Remote debugging will:"
  echo "  - Expose debugger port ${DEBUGPY_PORT}"
  echo "  - Expose FastAPI endpoint on port ${PORT}"
  echo "  - Allow remote code execution if accessed"
  echo ""
  read -p "Enable debug mode? Selecting N starts validator normally. [y/N]: " -n 1 -r
  echo ""

  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    unset DEBUGPY_PORT
    echo "Starting validator in normal mode..."
  else
    echo ""
    echo "Generating API key for secure access..."

    # Generate a random 32-character API key
    VALIDATOR_DEBUG_API_KEY=$(openssl rand -hex 32)

    # Export it for the current session
    export VALIDATOR_DEBUG_API_KEY

    # Update the .env file with the new API key
    if grep -q "^VALIDATOR_DEBUG_API_KEY=" "$PROJECT_ROOT/gittensor/validator/.env"; then
      # Update existing key
      sed -i "s|^VALIDATOR_DEBUG_API_KEY=.*|VALIDATOR_DEBUG_API_KEY=${VALIDATOR_DEBUG_API_KEY}|" "$PROJECT_ROOT/gittensor/validator/.env"
    else
      # Add new key
      echo "" >> "$PROJECT_ROOT/gittensor/validator/.env"
      echo "# Auto-generated API key for debug endpoints (regenerated on each debug start)" >> "$PROJECT_ROOT/gittensor/validator/.env"
      echo "VALIDATOR_DEBUG_API_KEY=${VALIDATOR_DEBUG_API_KEY}" >> "$PROJECT_ROOT/gittensor/validator/.env"
    fi

    echo "Debugpy enabled on port ${DEBUGPY_PORT} (attach whenever, no wait)"
    echo "Debug API will be available at http://localhost:${PORT}/trigger_scoring"
    echo ""
    echo "========================================="
    echo "🔑 YOUR API KEY (save this!):"
    echo "========================================="
    echo "${VALIDATOR_DEBUG_API_KEY}"
    echo "========================================="
    echo ""
    # Get server IP address
    SERVER_IP=$(hostname -I | awk '{print $1}')

    echo "Usage examples:"
    echo "  Health check:"
    echo "    curl -H \"X-API-Key: ${VALIDATOR_DEBUG_API_KEY}\" http://${SERVER_IP}:${PORT}/health"
    echo ""
    echo "  Trigger scoring:"
    echo "    curl -X POST -H \"X-API-Key: ${VALIDATOR_DEBUG_API_KEY}\" http://${SERVER_IP}:${PORT}/trigger_scoring"
    echo "========================================="
    echo ""

    DEBUGPY_PREFIX="-m debugpy --listen 0.0.0.0:${DEBUGPY_PORT}"
    # Enable the API endpoint (uses same port as validator)
    OPTIONAL_FLAGS="$OPTIONAL_FLAGS --neuron.remote_debug_port ${PORT}"
    # Add kill timeout to allow debugpy to cleanly release ports
    PM2_EXTRA_FLAGS="--kill-timeout 5000"
  fi
fi

# Login to Weights & Biases
if ! wandb login $WANDB_API_KEY; then
  echo "Failed to login to Weights & Biases with the provided API key."
  exit 1
fi

# STOP VALIDATOR PROCESS
if pm2 describe "$VALIDATOR_PROCESS_NAME" >/dev/null 2>&1; then
  echo "Process '$VALIDATOR_PROCESS_NAME' is already running. Killing and rerunning..."
  pm2 delete "$VALIDATOR_PROCESS_NAME" || pm2 delete "$VALIDATOR_PROCESS_NAME" --silent || true	
  pm2 start python --name "$VALIDATOR_PROCESS_NAME" $PM2_EXTRA_FLAGS -- $DEBUGPY_PREFIX neurons/validator.py --netuid $NETUID --subtensor.network $SUBTENSOR_NETWORK --wallet.name $WALLET_NAME --wallet.hotkey $HOTKEY_NAME --axon.port $PORT $OPTIONAL_FLAGS $LOGGING
else
  echo "Process '$VALIDATOR_PROCESS_NAME' is not running. Starting it for the first time..."
  pm2 start python --name "$VALIDATOR_PROCESS_NAME" $PM2_EXTRA_FLAGS -- $DEBUGPY_PREFIX neurons/validator.py --netuid $NETUID --subtensor.network $SUBTENSOR_NETWORK --wallet.name $WALLET_NAME --wallet.hotkey $HOTKEY_NAME --axon.port $PORT $OPTIONAL_FLAGS $LOGGING
fi
