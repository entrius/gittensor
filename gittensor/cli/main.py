# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Gittensor CLI - Main Entry Point

This module provides the main CLI entry point for Gittensor miner commands.

Usage:
    gittensor-cli --help
    gittensor-cli issue --help
"""

import click
from .issue_commands import issue


@click.group()
@click.version_option(version='0.1.0', prog_name='gittensor-cli')
def cli():
    """
    Gittensor CLI - Tools for miners on Subnet 74.

    Manage your participation in the Gittensor network, including
    issue competitions and preferences.
    """
    pass


# Register subcommands
cli.add_command(issue)


def main():
    """Main entry point for the CLI."""
    cli()


if __name__ == '__main__':
    main()
