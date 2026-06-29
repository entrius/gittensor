# Entrius 2025

"""
CLI commands for miner PAT management.

Command structure:
    gitt miner (alias: m)     - Miner management commands
        post                     Broadcast GitHub PAT to validators
        check                    Check how many validators have your PAT
        score                    Score GitHub entities through the production pipeline
        advisor                  Recommend score improvements from a local pipeline run
        scan                     Rank open issues across whitelisted repos by opportunity
"""

import click

from .advisor import advisor_command
from .check import miner_check
from .post import miner_post
from .scan import scan_command
from .score import score_command


@click.group(name='miner')
def miner_group():
    """Miner management commands.

    \b
    Commands:
        post     Broadcast your GitHub PAT to validators
        check    Check how many validators have your PAT stored
        score    Run the validator scoring pipeline end-to-end for a single miner
        advisor  Turn a local pipeline run into prioritized recommendations
        scan     Rank open issues across whitelisted repos by opportunity
    """
    pass


miner_group.add_command(miner_post, name='post')
miner_group.add_command(miner_check, name='check')
miner_group.add_command(score_command, name='score')
miner_group.add_command(advisor_command, name='advisor')
miner_group.add_command(scan_command, name='scan')


def register_miner_commands(cli):
    """Register miner commands with the root CLI group."""
    cli.add_command(miner_group, name='miner')
    cli.add_alias('miner', 'm')
