# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Gittensor CLI - Issue Competition Commands

This module provides CLI commands for miners to interact with the
Issues Competition system.

Usage:
    gitt issue list          # View available issues
    gitt issue prefer 1 2 3  # Set ranked preferences
    gitt issue status        # View current status
    gitt issue withdraw      # Clear preferences
    gitt issue elo           # View ELO rating
"""

from .issue_commands import issue, register_issue_commands

__all__ = ['issue', 'register_issue_commands']
