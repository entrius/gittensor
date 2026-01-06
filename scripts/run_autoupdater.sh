#!/bin/bash

# Exit on error
set -e

# Get the absolute path of the project root
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Change to project root
cd "$PROJECT_ROOT"

# Default values
PROCESSES="validator"
LOG_LEVEL="DEBUG"
CHECK_INTERVAL=420

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --processes)
            PROCESSES="$2"
            shift 2
            ;;
        --log-level)
            LOG_LEVEL="$2"
            shift 2
            ;;
        --check-interval)
            CHECK_INTERVAL="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --processes PROCS       Comma-separated processes to manage (default: validator)"
            echo "                         Options: validator, miner, validator,miner"
            echo "  --log-level LEVEL       Log level (default: INFO)"
            echo "  --check-interval SECS   Update check interval in seconds (default: 900)"
            echo "  --help, -h             Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                                    # Manage validator only"
            echo "  $0 --processes miner                  # Manage miner only"
            echo "  $0 --processes validator,miner        # Manage both"
            echo "  $0 --log-level DEBUG                  # Enable debug logging"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Check if virtual environment exists
VENV_PATH="$PROJECT_ROOT/gittensor-venv"
if [ ! -d "$VENV_PATH" ]; then
    echo "Virtual environment not found at $VENV_PATH"
    echo "Please run ./scripts/setup_env.sh first"
    exit 1
fi

# Check if PM2 is installed
if ! command -v pm2 &> /dev/null; then
    echo "PM2 is not installed run ./scripts/setup_env.sh first"
    echo "sudo npm install -g pm2@latest"
    exit 1
fi

# Start auto-updater with PM2
echo "Starting GitTensor auto-updater for processes: $PROCESSES"
pm2 start "$VENV_PATH/bin/python" \
    --name "gt-autoupdater" \
    -- auto_updater/auto_updater_main.py \
    --processes "$PROCESSES" \
    --log-level "$LOG_LEVEL" \
    --check-interval "$CHECK_INTERVAL"

echo ""
echo "Auto-updater started successfully!"
echo ""
echo "Useful commands:"
echo "  pm2 logs gt-autoupdater    # View logs"
echo "  pm2 stop gt-autoupdater    # Stop auto-updater"
echo "  pm2 restart gt-autoupdater # Restart auto-updater"
echo "  pm2 delete gt-autoupdater  # Remove auto-updater"
