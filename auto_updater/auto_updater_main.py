"""
Auto-Update Service Main Entry Point

This is the main entry point for the GitTensor auto-update service.
It runs as a separate PM2 process and manages updates for validator and miner processes.

Usage:
    python core/auto_updater_main.py [--config config.json] [--processes validator,miner]
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from typing import Optional

from auto_update import AutoUpdateConfig, AutoUpdateManager, ManagedProcess

# Global manager instance
manager: Optional[AutoUpdateManager] = None


def setup_logging(log_level: str = "INFO") -> None:
    """Setup logging configuration."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler('auto_updater.log')],
    )


def load_config(config_path: Optional[str] = None) -> AutoUpdateConfig:
    """Load configuration from file or use defaults."""
    logger = logging.getLogger(__name__)

    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config_data = json.load(f)

            logger.info(f"Loaded configuration from {config_path}")

            # Parse processes from config
            processes = []
            for proc_data in config_data.get('processes', []):
                processes.append(
                    ManagedProcess(
                        name=proc_data['name'],
                        neuron_type=proc_data['neuron_type'],
                        pm2_process_name=proc_data['pm2_process_name'],
                        enabled=proc_data.get('enabled', True),
                    )
                )

            return AutoUpdateConfig(
                check_interval_seconds=config_data.get('check_interval_seconds', 900),
                enabled=config_data.get('enabled', True),
                max_consecutive_failures=config_data.get('max_consecutive_failures', 3),
                failure_cooldown_seconds=config_data.get('failure_cooldown_seconds', 1800),
                processes=processes,
            )

        except Exception as e:
            logger.error(f"Failed to load config from {config_path}: {e}")
            logger.info("Using default configuration")

    # Default configuration
    return AutoUpdateConfig()


def create_processes_from_args(process_names: str) -> list[ManagedProcess]:
    """Create managed processes from command line arguments."""
    processes = []

    for name in process_names.split(','):
        name = name.strip().lower()

        if name == 'validator':
            processes.append(
                ManagedProcess(
                    name='gt-vali', neuron_type='validator', pm2_process_name='gt-vali'  # From run_validator.sh
                )
            )
        elif name == 'miner':
            processes.append(ManagedProcess(name='gt-miner', neuron_type='miner', pm2_process_name='gt-miner'))
        else:
            logging.getLogger(__name__).warning(f"Unknown process type: {name}")

    return processes


def signal_handler(signum: int, frame) -> None:
    """Handle shutdown signals."""
    logger = logging.getLogger(__name__)
    logger.info(f"Received signal {signum}, shutting down...")

    global manager
    if manager:
        manager.stop()

    sys.exit(0)


def setup_update_callbacks(manager: AutoUpdateManager) -> None:
    """Setup callbacks for update events."""
    logger = logging.getLogger(__name__)

    def on_update_started(process_name: str):
        logger.info(f"🔄 Update started for {process_name}")

    def on_update_completed(process_name: str, success: bool):
        if success:
            logger.info(f"✅ Update completed successfully for {process_name}")
        else:
            logger.error(f"❌ Update failed for {process_name}")

    def on_update_failed(process_name: str, error: str):
        logger.error(f"💥 Update failed for {process_name}: {error}")

    manager.on_update_started = on_update_started
    manager.on_update_completed = on_update_completed
    manager.on_update_failed = on_update_failed


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='GitTensor Auto-Update Service')
    parser.add_argument('--config', type=str, help='Path to configuration file')
    parser.add_argument(
        '--processes',
        type=str,
        default='validator,miner',
        help='Comma-separated list of processes to manage (validator,miner)',
    )
    parser.add_argument(
        '--log-level', type=str, default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help='Logging level'
    )
    parser.add_argument(
        '--check-interval', type=int, default=900, help='Update check interval in seconds (default: 900)'
    )
    parser.add_argument('--disable', action='store_true', help='Start with auto-update disabled (for testing)')

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    logger.info("🚀 Starting GitTensor Auto-Update Service")

    # Load configuration
    config = load_config(args.config)

    # Override config with command line arguments
    if args.check_interval != 900:
        config.check_interval_seconds = args.check_interval

    if args.disable:
        config.enabled = False
        logger.info("Auto-update disabled via command line")

    # Add processes from command line if no config file processes
    if not config.processes and args.processes:
        config.processes = create_processes_from_args(args.processes)

    if not config.processes:
        logger.error("No processes configured for auto-update")
        sys.exit(1)

    # Create manager
    global manager
    manager = AutoUpdateManager(config)

    # Setup callbacks
    setup_update_callbacks(manager)

    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Log configuration
    status = manager.get_status()
    logger.info(f"Configuration:")
    logger.info(f"  - Enabled: {status['enabled']}")
    logger.info(f"  - Check interval: {status['check_interval']}s")
    logger.info(f"  - Managed processes: {status['managed_processes']}")

    for proc in status['processes']:
        logger.info(f"    - {proc['name']} ({proc['neuron_type']}) -> {proc['pm2_process_name']}")

    # Start the manager
    if not manager.start():
        logger.error("Failed to start auto-update manager")
        sys.exit(1)

    # Keep the main thread alive
    try:
        while True:
            time.sleep(60)

            # Periodically log status
            if int(time.time()) % 3600 == 0:  # Every hour
                status = manager.get_status()
                logger.info(
                    f"Status: Running={status['running']}, "
                    f"Failures={status['consecutive_failures']}, "
                    f"Cooldown={status['in_cooldown']}"
                )

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        signal_handler(signal.SIGTERM, None)


if __name__ == '__main__':
    main()

