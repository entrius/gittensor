# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
CLI commands for validator operations.

Command structure:
    gitt validator (alias: v)   - Validator operations
        weights (alias: w)          Repo weight basket voting
            show                        Your basket + all published baskets
            set                         Publish a basket (repo=weight pairs)
            clear                       Clear your basket
"""

import click

from gittensor.cli.core.help import StyledAliasGroup

from .weights import weights_group


@click.group(name='validator', cls=StyledAliasGroup)
def validator_group():
    """Validator operations: repo weight basket voting."""
    pass


validator_group.add_command(weights_group, name='weights')
validator_group.add_alias('weights', 'w')


def register_validator_commands(cli):
    """Register validator commands with the root CLI group."""
    cli.add_command(validator_group, name='validator')
    cli.add_alias('validator', 'v')


__all__ = ['register_validator_commands', 'validator_group', 'weights_group']
