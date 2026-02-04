# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
CLI commands for managing issue bounties

Command structure:
    gitt view (alias: v)         - Read commands (contract + API)
    gitt register (alias: reg)   - Registration commands
    gitt harvest                 - Harvest emissions (top-level)
    gitt val                     - Validator consensus commands
    gitt admin (alias: a)        - Owner-only commands
"""

import click

from .view import view
from .val import val
from .admin import admin
from .mutations import (
    issue_register,
    issue_harvest,
)

# Re-export helpers
from .helpers import (
    console,
    load_config,
    get_contract_address,
    get_ws_endpoint,
    get_api_url,
    read_issues_from_contract,
    GITTENSOR_DIR,
    CONFIG_FILE,
    DEFAULT_API_URL,
)


# Create register group for `gitt register issue`
@click.group(name='register')
def register_group():
    """Registration commands.

    \b
    Commands:
        issue    Register a new issue bounty
    """
    pass


# Add issue_register as 'issue' subcommand under register
register_group.add_command(issue_register, name='issue')


def register_commands(cli):
    """Register all issue-related commands with the root CLI group."""
    # View group and alias
    cli.add_command(view)
    cli.add_command(view, name='v')

    # Register group and alias
    cli.add_command(register_group, name='register')
    cli.add_command(register_group, name='reg')

    # Harvest as top-level command (no subgroup)
    cli.add_command(issue_harvest, name='harvest')

    # Validator group
    cli.add_command(val)

    # Admin group and alias
    cli.add_command(admin)
    cli.add_command(admin, name='a')


__all__ = [
    'register_commands',
    'view',
    'val',
    'admin',
    'register_group',
    'issue_harvest',
    # Helpers
    'console',
    'load_config',
    'get_contract_address',
    'get_ws_endpoint',
    'get_api_url',
    'read_issues_from_contract',
    'GITTENSOR_DIR',
    'CONFIG_FILE',
    'DEFAULT_API_URL',
]
