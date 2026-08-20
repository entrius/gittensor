"""
GitTensor Utilities
"""


def backoff_seconds(attempt: int, base: int = 5, cap: int = 30) -> int:
    return min(base * (2**attempt), cap)
