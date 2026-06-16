# Entrius 2025

"""
CLI commands for miner PAT management.

Command structure:
    gitt miner (alias: m)     - Miner management commands
        post                     Broadcast GitHub PAT to validators
        check                    Check how many validators have your PAT
        ensure                   Re-broadcast PAT only to validators missing it
        score                    Score GitHub entities through the production pipeline
"""

import click

from .check import miner_check
from .ensure import miner_ensure
from .post import miner_post
from .score import score_command


@click.group(name='miner')
def miner_group():
    """Miner management commands.

    \b
    Commands:
        post     Broadcast your GitHub PAT to validators
        check    Check how many validators have your PAT stored
        ensure   Re-broadcast your PAT only to validators missing it (cron-safe)
        score    Run the validator scoring pipeline end-to-end for a single miner
    """
    pass


miner_group.add_command(miner_post, name='post')
miner_group.add_command(miner_check, name='check')
miner_group.add_command(miner_ensure, name='ensure')
miner_group.add_command(score_command, name='score')


def register_miner_commands(cli):
    """Register miner commands with the root CLI group."""
    cli.add_command(miner_group, name='miner')
    cli.add_alias('miner', 'm')
