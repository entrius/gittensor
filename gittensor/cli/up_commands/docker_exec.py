# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The one place `gitt up` / `gitt down` shell out to docker (tests patch `run_docker`)."""

from __future__ import annotations

import subprocess


def run_docker(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)
