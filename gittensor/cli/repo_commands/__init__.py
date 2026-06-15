# Entrius 2025

"""
CLI commands for repository emission-weight tooling.

Command structure:
    gitt repo (alias: r)      - Repository weight tooling
        simulate                 Preview the emission/reward split for a proposed
                                 master_repositories.json before committing a reweight
"""

import click

from gittensor.cli.issue_commands.help import StyledGroup

from .simulate import simulate_command


@click.group(name='repo', cls=StyledGroup)
def repo_group():
    """Repository emission-weight tooling.

    \b
    Commands:
        simulate    Preview the emission/reward split for a proposed
                    master_repositories.json before committing a reweight
    """
    pass


repo_group.add_command(simulate_command, name='simulate')


def register_repo_commands(cli):
    """Register repo commands with the root CLI group."""
    cli.add_command(repo_group, name='repo')
    cli.add_alias('repo', 'r')
