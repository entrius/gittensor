# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
CLI commands for the compute agent (the whole miner, vault 23 §2).

Command structure:
    gitt up      - Check prerequisites and start the agent container (self-updating runner)
    gitt down    - Stop the agent and its runner
"""

from .down import down_command
from .up import up_command


def register_up_commands(cli):
    """Register `gitt up` / `gitt down` with the root CLI group."""
    cli.add_command(up_command, name='up')
    cli.add_command(down_command, name='down')
