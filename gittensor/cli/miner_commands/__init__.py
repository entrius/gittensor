# Entrius 2025

"""
CLI commands for miner PAT management.

Command structure:
    gitt miner (alias: m)     - Miner management commands
        post                     Broadcast GitHub PAT to validators
        check                    Check how many validators have your PAT
        languages                List file extensions ranked by scoring weight
"""

import click

from .check import miner_check
from .languages import miner_languages
from .post import miner_post


@click.group(name='miner')
def miner_group():
    """Miner management commands.

    \b
    Commands:
        post         Broadcast your GitHub PAT to validators
        check        Check how many validators have your PAT stored
        languages    List file extensions ranked by scoring weight and method
    """
    pass


miner_group.add_command(miner_post, name='post')
miner_group.add_command(miner_check, name='check')
miner_group.add_command(miner_languages, name='languages')


def register_miner_commands(cli):
    """Register miner commands with the root CLI group."""
    cli.add_command(miner_group, name='miner')
    cli.add_alias('miner', 'm')
