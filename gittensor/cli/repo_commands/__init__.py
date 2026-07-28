# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
CLI commands for the on-chain repository registry.

Command structure:
    gitt repo (alias: r)        - Repository registry commands
        register                    Register a repository (dynamic alpha fee)
        list                        List registered repositories
        show                        Show one repo + params + labels + patterns
        price                       Current registration price quote
        set-params                  Set hyperparams (name=value pairs)
        set-label / remove-label    Manage label multipliers
        set-branch-patterns         Replace acceptable branch patterns
        update-name                 Follow a GitHub rename
        transfer                    Transfer repo ownership
        deregister                  Free the slot (no refund)
"""

import click

from gittensor.cli.core.help import StyledAliasGroup

from .mutations import (
    repo_deregister,
    repo_register,
    repo_remove_label,
    repo_set_branch_patterns,
    repo_set_label,
    repo_set_params,
    repo_transfer,
    repo_update_name,
)
from .view import repo_list, repo_price, repo_show


@click.group(name='repo', cls=StyledAliasGroup)
def repo_group():
    """Manage the on-chain repository registry."""
    pass


repo_group.add_command(repo_register, name='register')
repo_group.add_command(repo_list, name='list')
repo_group.add_command(repo_show, name='show')
repo_group.add_command(repo_price, name='price')
repo_group.add_command(repo_set_params, name='set-params')
repo_group.add_command(repo_set_label, name='set-label')
repo_group.add_command(repo_remove_label, name='remove-label')
repo_group.add_command(repo_set_branch_patterns, name='set-branch-patterns')
repo_group.add_command(repo_update_name, name='update-name')
repo_group.add_command(repo_transfer, name='transfer')
repo_group.add_command(repo_deregister, name='deregister')


def register_repo_commands(cli):
    """Register repo registry commands with the root CLI group."""
    cli.add_command(repo_group, name='repo')
    cli.add_alias('repo', 'r')


__all__ = ['register_repo_commands', 'repo_group']
