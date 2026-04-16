# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared filesystem paths for CLI commands."""

import json
from pathlib import Path
from typing import Any, Optional

GITTENSOR_DIR = Path.home() / '.gittensor'
CONFIG_FILE = GITTENSOR_DIR / 'config.json'


def load_config_value(key: str) -> Optional[Any]:
    """Load a single key from ``~/.gittensor/config.json``, or ``None`` if missing or invalid."""
    if not CONFIG_FILE.exists():
        return None
    try:
        config = json.loads(CONFIG_FILE.read_text())
        return config.get(key)
    except (json.JSONDecodeError, OSError):
        return None
