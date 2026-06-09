# The MIT License (MIT)
# Copyright 2025 Entrius

"""Lightweight exception types for issue-competition contract operations.

Kept in a separate module so callers (CLI, tests) can import them without
pulling in the heavy ``contract_client`` dependency tree.
"""


class TreasuryReadError(RuntimeError):
    """Raised when the treasury stake cannot be read due to a chain or network error.

    Distinct from a genuine zero-stake treasury, where ``get_treasury_stake``
    returns ``0`` normally.  Callers (e.g. ``pending-harvest --json``) must
    treat this as a read failure and emit ``success: false``.
    """
