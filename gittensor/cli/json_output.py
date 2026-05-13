# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Canonical JSON output helpers for the CLI (envelope shape, argv detection,
Click-exception mapping)."""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Optional

import click


def emit_json(payload: Any, pretty: bool = True, default: Optional[Callable] = str) -> None:
    """Write a JSON payload to stdout (pretty by default; compact when `pretty=False`)"""
    if pretty:
        output = json.dumps(payload, indent=2, ensure_ascii=False, default=default)
    else:
        output = json.dumps(payload, separators=(',', ':'), ensure_ascii=False, default=default)
    click.echo(output, err=False)


def emit_error_json(message: str, error_type: str = 'cli_error', **extra: Any) -> None:
    """Write `{success: false, error: {type, message}, ...extra}` to stdout.

    Extra kwargs become sibling fields (e.g. `skipped=[...]`).
    """
    payload: dict[str, Any] = {
        'success': False,
        'error': {'type': error_type, 'message': message},
        **extra,
    }
    click.echo(json.dumps(payload), err=False)


def wants_json_output(argv: Iterable[str]) -> bool:
    """True if `--json` appears anywhere in argv"""
    return any(t == '--json' or t.startswith('--json=') for t in argv)


def click_error_type(exc: click.ClickException) -> str:
    """Map a Click exception class to the canonical `error_type` tag"""
    if isinstance(exc, click.BadParameter):
        return 'bad_parameter'
    if isinstance(exc, click.UsageError):
        return 'usage_error'
    return 'cli_error'
