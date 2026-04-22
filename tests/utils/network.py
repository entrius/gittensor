# Entrius 2025

"""Network / endpoint helpers (non-constant logic shared by CLI and tooling)."""


def looks_like_chain_endpoint(value: str) -> bool:
    """Return True if ``value`` looks like a WebSocket or HTTP RPC endpoint.

    Used so unknown ``--network`` strings are not mistaken for endpoints (e.g.
    a typo ``finny`` instead of ``finney``).
    """
    v = value.strip().lower()
    return v.startswith(('ws://', 'wss://', 'http://', 'https://'))
