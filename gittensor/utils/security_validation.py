"""
Repository URL Validation Module
================================
Security improvements for Gittensor - SSRF protection and input validation.
"""

import re
from typing import Optional


def validate_repository_name(repo: str) -> bool:
    """
    Validate repository name format to prevent SSRF and injection attacks.
    
    Args:
        repo: Repository in format 'owner/repo'
    
    Returns:
        True if valid, False otherwise
    
    Security:
        - Prevents SSRF by blocking IP addresses
        - Prevents path traversal with '..'
        - Only allows alphanumeric, hyphens, underscores, periods, and slashes
    """
    if not repo or not isinstance(repo, str):
        return False
    
    # Must be in owner/repo format
    if '/' not in repo:
        return False
    
    owner, repo_name = repo.split('/', 1)
    
    # Owner and repo name validation
    # GitHub allows: alphanumeric, hyphens, underscores, periods
    # Length: owner 1-39, repo 1-100
    pattern = r'^[a-zA-Z0-9]([a-zA-Z0-9\-_\.]*[a-zA-Z0-9])?$'
    
    if not re.match(pattern, owner) or len(owner) > 39:
        return False
    
    if not re.match(pattern, repo_name) or len(repo_name) > 100:
        return False
    
    # Explicitly block path traversal
    if '..' in repo:
        return False
    
    # Block potential IP addresses or internal hosts
    internal_patterns = [
        r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$',  # IPv4
        r'^localhost',
        r'^127\.',
        r'^10\.',
        r'^172\.(1[6-9]|2[0-9]|3[0-1])\.',
        r'^192\.168\.',
    ]
    
    for pattern in internal_patterns:
        if re.match(pattern, owner, re.IGNORECASE):
            return False
    
    return True


def sanitize_repository_name(repo: str) -> Optional[str]:
    """
    Sanitize and validate a repository name.
    
    Args:
        repo: Repository name to sanitize
    
    Returns:
        Sanitized repository name or None if invalid
    """
    if not validate_repository_name(repo):
        return None
    return repo


def validate_issue_number(issue_number: int) -> bool:
    """Validate issue/PR number is within GitHub limits."""
    return isinstance(issue_number, int) and 1 <= issue_number <= 999999999
