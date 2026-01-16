#!/bin/bash
set -e

# Gittensor Validator Docker Entrypoint
# Handles environment variable expansion to CLI args and graceful shutdown

# Trap SIGTERM for graceful shutdown
shutdown() {
    echo "Received shutdown signal, stopping validator..."
    if [ -n "$VALIDATOR_PID" ]; then
        kill -TERM "$VALIDATOR_PID" 2>/dev/null || true
        wait "$VALIDATOR_PID" 2>/dev/null || true
    fi
    exit 0
}
trap shutdown SIGTERM SIGINT

# Validate required environment variables
check_required_env() {
    local missing=()

    [ -z "$NETUID" ] && missing+=("NETUID")
    [ -z "$WALLET_NAME" ] && missing+=("WALLET_NAME")
    [ -z "$HOTKEY_NAME" ] && missing+=("HOTKEY_NAME")
    [ -z "$WANDB_API_KEY" ] && missing+=("WANDB_API_KEY")

    if [ ${#missing[@]} -gt 0 ]; then
        echo "Error: Missing required environment variables:"
        printf '  - %s\n' "${missing[@]}"
        echo ""
        echo "Please set these in your .env file or docker-compose.yml"
        exit 1
    fi
}

# Login to Weights & Biases
wandb_login() {
    echo "Logging in to Weights & Biases..."
    if ! wandb login "$WANDB_API_KEY" 2>/dev/null; then
        echo "Warning: Failed to login to Weights & Biases"
        echo "Validator will continue but W&B logging may not work"
    fi
}

# Build command line arguments from environment variables
build_args() {
    local args=()

    # Required arguments
    args+=("--netuid" "$NETUID")
    args+=("--wallet.name" "$WALLET_NAME")
    args+=("--wallet.hotkey" "$HOTKEY_NAME")

    # Network configuration (default: finney)
    args+=("--subtensor.network" "${SUBTENSOR_NETWORK:-finney}")

    # Port configuration (default: 8099)
    args+=("--axon.port" "${PORT:-8099}")

    # Logging level
    case "${LOG_LEVEL:-info}" in
        debug)
            args+=("--logging.debug")
            ;;
        trace)
            args+=("--logging.trace")
            ;;
        *)
            args+=("--logging.info")
            ;;
    esac

    # Optional: Database storage
    if [ "${STORE_DB_RESULTS:-false}" = "true" ]; then
        args+=("--database.store_validation_results")
    fi

    # Optional: Remote debug port (for development)
    if [ -n "$REMOTE_DEBUG_PORT" ]; then
        args+=("--neuron.remote_debug_port" "$REMOTE_DEBUG_PORT")
    fi

    echo "${args[@]}"
}

# Main execution
main() {
    local mode="${1:-validator}"

    case "$mode" in
        validator)
            check_required_env
            wandb_login

            echo "Starting Gittensor Validator..."
            echo "  Network: ${SUBTENSOR_NETWORK:-finney}"
            echo "  NetUID: $NETUID"
            echo "  Wallet: $WALLET_NAME"
            echo "  Hotkey: $HOTKEY_NAME"
            echo "  Port: ${PORT:-8099}"
            echo ""

            # Build arguments and start validator
            ARGS=$(build_args)
            echo "Running: python neurons/validator.py $ARGS"
            echo ""

            # Run validator in background and wait for it
            python neurons/validator.py $ARGS &
            VALIDATOR_PID=$!
            wait "$VALIDATOR_PID"
            ;;

        shell)
            # Debug mode - drop into shell
            exec /bin/bash
            ;;

        *)
            # Pass through any other command
            exec "$@"
            ;;
    esac
}

main "$@"
