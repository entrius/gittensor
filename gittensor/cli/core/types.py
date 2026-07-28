# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Click ParamTypes shared across CLI command groups (purely syntactic checks)."""

from __future__ import annotations

import click

from .helpers import REPO_PATTERN, validate_ss58_address


class RepoNameType(click.ParamType):
    """Owner/repo string matching ``REPO_PATTERN``."""

    name = 'repo'

    def convert(self, value, param, ctx):
        trimmed = value.strip()
        if not REPO_PATTERN.match(trimmed):
            self.fail(f"'{value}' is not a valid owner/repo", param, ctx)
        return trimmed


class SS58AddressType(click.ParamType):
    """SS58 address validated via ``validate_ss58_address``."""

    name = 'ss58'

    def convert(self, value, param, ctx):
        name = (param.name if param else None) or 'address'
        try:
            return validate_ss58_address(value, name)
        except click.BadParameter as exc:
            self.fail(str(exc), param, ctx)


# Stateless singletons - one instance per type for the whole CLI
REPO = RepoNameType()
SS58 = SS58AddressType()
