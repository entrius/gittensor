# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Gittensor CLI - Issue Competition Commands

This module provides CLI commands for miners to interact with the
Issues Competition system.

Usage:
    gittensor-cli issue list          # View available issues
    gittensor-cli issue prefer 1 2 3  # Set ranked preferences
    gittensor-cli issue status        # View current status
    gittensor-cli issue withdraw      # Clear preferences
    gittensor-cli issue elo           # View ELO rating
"""

from .issue_commands import issue, register_issue_commands

__all__ = ['issue', 'register_issue_commands']
