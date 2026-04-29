# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for gittensor.utils.uids.get_all_uids."""

from types import SimpleNamespace

import pytest

from gittensor.utils.uids import get_all_uids


class _FakeMetagraphSize:
    """Mimic the bittensor metagraph's `n.item()` interface."""

    def __init__(self, n: int):
        self._n = n

    def item(self) -> int:
        return self._n


def _make_self(n: int) -> SimpleNamespace:
    return SimpleNamespace(metagraph=SimpleNamespace(n=_FakeMetagraphSize(n)))


def test_get_all_uids_returns_full_range():
    result = get_all_uids(_make_self(5))
    assert result == {0, 1, 2, 3, 4}
    assert isinstance(result, set)


def test_get_all_uids_always_includes_uid_zero_even_for_empty_metagraph():
    """Bootstrap invariant: the empty-metagraph case must still yield {0}."""
    result = get_all_uids(_make_self(0))
    assert result == {0}


def test_get_all_uids_for_single_uid_metagraph():
    result = get_all_uids(_make_self(1))
    assert result == {0}


def test_get_all_uids_takes_no_extra_arguments():
    """Regression guard: the dead `exclude` parameter must not return."""
    with pytest.raises(TypeError):
        get_all_uids(_make_self(3), exclude=[1])  # type: ignore[call-arg]
