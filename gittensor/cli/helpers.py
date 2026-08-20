# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared CLI helpers: consoles, config location, common click choices."""

from pathlib import Path

import click
from rich.console import Console

console = Console()
err_console = Console(stderr=True)

GITTENSOR_DIR = Path.home() / '.gittensor'
CONFIG_FILE = GITTENSOR_DIR / 'config.json'

NETWORK_CHOICE = click.Choice(['finney', 'test', 'local'], case_sensitive=False)
