# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Gittensor CLI - Main Entry Point

This module provides the main CLI entry point for Gittensor miner commands.

Usage:
    gitt --help
    gitt issue --help
"""

import click
from .issue_commands import issue
from .config_commands import config


@click.group()
@click.version_option(version='0.1.0', prog_name='gitt')
def cli():
    """
    Gittensor CLI - Tools for miners on Subnet 74.

    Manage your participation in the Gittensor network, including
    issue competitions and preferences.
    """
    pass


# Register subcommands
cli.add_command(issue)
cli.add_command(config)


def main():
    """Main entry point for the CLI."""
    cli()


if __name__ == '__main__':
    main()
