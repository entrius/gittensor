# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
CLI commands for managing issue bounties

Command structure:
    gitt issue (alias: i)           - Top-level mutation commands
    gitt issue view (alias: v)      - Read commands (contract + API)
    gitt issue val                  - Validator consensus commands
    gitt issue admin (alias: a)     - Owner-only commands
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


@click.group()
def issue():
    """Issue bounty commands

    Manage issue bounties for GitHub issues. Miners who solve issues
    receive ALPHA token bounties.

    \b
    Subcommands:
        view    Read contract state and API data (alias: v)
        val     Validator consensus operations
        admin   Owner-only commands (alias: a)

    \b
    Examples:
        gitt issue register --repo owner/repo --issue 1 --bounty 100
        gitt issue view issues --testnet
        gitt i v bounty-pool
    """
    pass


# Register subgroups
issue.add_command(view)
issue.add_command(view, name='v')  # Alias
issue.add_command(val)
issue.add_command(admin)
issue.add_command(admin, name='a')  # Alias

# Register top-level mutation commands
issue.add_command(issue_register, name='register')
issue.add_command(issue_harvest, name='harvest')


def register_issue_commands(cli):
    """Register issue commands with a parent CLI group."""
    cli.add_command(issue)
    # Register 'i' alias at the parent level
    cli.add_command(issue, name='i')


__all__ = [
    'issue',
    'register_issue_commands',
    'view',
    'val',
    'admin',
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
