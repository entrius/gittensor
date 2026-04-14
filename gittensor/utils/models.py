"""Shared typed models used across utils and CLI modules."""

from typing import List, Optional, TypedDict


class ClosingIssue(TypedDict):
    """Metadata for an issue closed by a PR."""
    number: int
    is_transferred: bool


class PRInfo(TypedDict, total=False):
    """GitHub PR discovery model.

    This shape is intentionally minimal and stable so GraphQL and REST-based
    discovery paths can share one contract.
    """

    number: int
    title: str
    author_login: str
    author_id: Optional[int]
    created_at: str
    merged_at: Optional[str]
    state: str
    url: str
    review_count: int
    closing_numbers: List[int]
    closing_issues: List[ClosingIssue]
