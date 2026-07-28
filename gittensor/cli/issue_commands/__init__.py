# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
CLI commands for managing issue bounties

Command structure:
    gitt issues (alias: i)       - Issue management commands
        list                         List issues or view a specific issue
        submissions                  List open PR submissions for an issue
        register                     Register a new issue bounty
        bounty-pool                  View total bounty pool
        pending-harvest              View pending emissions
        harvest                      Harvest emissions
        vote                         Validator consensus commands
        admin (alias: a)             Owner-only commands
            info                         View contract configuration
            cancel-issue                 Cancel an issue
            payout-issue                 Manual payout fallback
            set-owner                    Transfer ownership
            set-treasury                 Change treasury hotkey
"""

import click

from gittensor.cli.core.help import StyledAliasGroup

from .admin import admin
from .mutations import (
    issue_harvest,
    issue_register,
)
from .submissions import issues_submissions
from .view import admin_info, issues_bounty_pool, issues_list, issues_pending_harvest
from .vote import vote


@click.group(name='issues', cls=StyledAliasGroup)
def issues_group():
    """Manage issue bounties, submissions, and predictions."""
    pass


issues_group.add_command(issues_list, name='list')
issues_group.add_command(issues_submissions, name='submissions')
issues_group.add_command(issue_register, name='register')
issues_group.add_command(issues_bounty_pool, name='bounty-pool')
issues_group.add_command(issues_pending_harvest, name='pending-harvest')
issues_group.add_command(issue_harvest, name='harvest')
issues_group.add_command(vote, name='vote')
issues_group.add_command(admin)
issues_group.add_alias('admin', 'a')

# Add info to admin group
admin.add_command(admin_info, name='info')


def register_commands(cli):
    """Register all issue-related commands with the root CLI group."""
    cli.add_command(issues_group, name='issues')
    cli.add_alias('issues', 'i')


__all__ = [
    'register_commands',
    'issues_group',
    'vote',
    'admin',
    'issues_submissions',
    'issue_register',
    'issue_harvest',
]
