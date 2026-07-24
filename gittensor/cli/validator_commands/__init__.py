# The MIT License (MIT)
# Copyright © 2025 Entrius

import click

from gittensor.cli.issue_commands.help import StyledGroup
from gittensor.cli.validator_commands.weights import weights_group


@click.group(name='validator', cls=StyledGroup)
def validator_group():
    """Validator operations."""


validator_group.add_command(weights_group)


def register_validator_commands(cli):
    cli.add_command(validator_group)
    cli.add_alias('validator', 'vali')
