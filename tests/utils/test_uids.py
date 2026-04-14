# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Unit tests for gittensor.utils.uids module."""

from unittest.mock import Mock

import pytest

uids_module = pytest.importorskip('gittensor.utils.uids', reason='Requires gittensor package')
get_all_uids = uids_module.get_all_uids


def _make_self(n: int):
    """Create a mock object mimicking self.metagraph.n.item()."""
    mock = Mock()
    mock.metagraph.n.item.return_value = n
    return mock


class TestGetAllUids:
    def test_returns_all_uids_when_no_exclusions(self):
        mock_self = _make_self(5)
        result = get_all_uids(mock_self)
        assert result == {0, 1, 2, 3, 4}

    def test_excludes_specified_uids(self):
        mock_self = _make_self(5)
        result = get_all_uids(mock_self, exclude=[1, 3])
        assert result == {0, 2, 4}

    def test_uid_zero_always_included_even_when_excluded(self):
        mock_self = _make_self(5)
        result = get_all_uids(mock_self, exclude=[0])
        assert 0 in result

    def test_default_exclude_not_shared_across_calls(self):
        """Regression test: mutable default argument (W0102) must not leak state.

        Before the fix, `exclude: List[int] = []` shared the same list object
        across all calls that relied on the default.  If any internal or external
        code mutated it, subsequent calls would silently inherit the mutation.
        """
        mock_self = _make_self(5)

        # First call — default exclude
        result1 = get_all_uids(mock_self)
        assert result1 == {0, 1, 2, 3, 4}

        # Second call — also default exclude, must return the same result
        result2 = get_all_uids(mock_self)
        assert result2 == {0, 1, 2, 3, 4}

    def test_empty_metagraph(self):
        mock_self = _make_self(0)
        result = get_all_uids(mock_self)
        # UID 0 is always forced in
        assert result == {0}
