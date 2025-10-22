"""
GitTensor Utilities
"""

import hashlib

def mask_secret(secret: str, length: int = 5) -> str:
    """Return a short SHA-256 hash of a secret for logging."""
    h = hashlib.sha256(str(secret).encode("utf-8")).hexdigest()
    return f"<masked:{h[:length]}>"