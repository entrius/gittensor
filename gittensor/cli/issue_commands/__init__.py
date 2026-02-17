# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
CLI commands for managing issue bounties

Command structure:
    gitt issues (alias: i)       - Issue management commands
        list [--id <ID>]             List issues or view a specific issue
        register                     Register a new issue bounty
        bounty-pool                  View total bounty pool
        pending-harvest              View pending emissions
    gitt harvest                 - Harvest emissions (top-level)
    gitt vote                    - Validator consensus commands
    gitt admin (alias: a)        - Owner-only commands
        info                         View contract configuration
        cancel-issue                 Cancel an issue
        payout-issue                 Manual payout fallback
        set-owner                    Transfer ownership
        set-treasury                 Change treasury hotkey
"""

import click

from .admin import admin

# Re-export helpers
from .helpers import (
    CONFIG_FILE,
    GITTENSOR_DIR,
    NETWORK_MAP,
    console,
    get_contract_address,
    load_config,
    read_issues_from_contract,
    resolve_network,
)
from .mutations import (
    issue_harvest,
    issue_register,
)
from .view import admin_info, issues_bounty_pool, issues_list, issues_pending_harvest
from .vote import vote


@click.group(name='issues')
def issues_group():
    """Issue management commands.

    \b
    Commands:
        list              List issues or view a specific issue
        register          Register a new issue bounty
        bounty-pool       View total bounty pool
        pending-harvest   View pending emissions
    """
    pass


issues_group.add_command(issues_list, name='list')
issues_group.add_command(issue_register, name='register')
issues_group.add_command(issues_bounty_pool, name='bounty-pool')
issues_group.add_command(issues_pending_harvest, name='pending-harvest')

# Add info to admin group
admin.add_command(admin_info, name='info')


def register_commands(cli):
    """Register all issue-related commands with the root CLI group."""
    # Issues group with alias
    cli.add_command(issues_group, name='issues')
    cli.add_alias('issues', 'i')

    # Harvest as top-level command
    cli.add_command(issue_harvest, name='harvest')

    # Validator vote group
    cli.add_command(vote, name='vote')

    # Admin group with alias
    cli.add_command(admin)
    cli.add_alias('admin', 'a')


__all__ = [
    'register_commands',
    'issues_group',
    'vote',
    'admin',
    'issue_register',
    'issue_harvest',
    # Helpers
    'console',
    'load_config',
    'get_contract_address',
    'resolve_network',
    'read_issues_from_contract',
    'GITTENSOR_DIR',
    'CONFIG_FILE',
    'NETWORK_MAP',
]
