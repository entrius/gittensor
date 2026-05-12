"""Tests for cached UID issue-discovery DB storage fix (#1052)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_cached_uid_stored_with_fresh_issue_discovery():
    """Cached UIDs should be stored with fresh issue-discovery fields."""
    from gittensor.validator.forward import forward

    mock_self = MagicMock()
    mock_self.step = 0
    mock_self.bulk_store_evaluation = AsyncMock()

    miner_evaluations = {
        1: type(
            'MockEval',
            (),
            {
                'uid': 1,
                'hotkey': 'test_hotkey',
                'github_id': '123',
                'issue_discovery_score': 0.5,
                'is_issue_eligible': True,
            },
        )()
    }

    with (
        patch('gittensor.validator.forward.oss_contributions', return_value=([], miner_evaluations, {1}, set())),
        patch('gittensor.validator.forward.issue_discovery', return_value=[]),
        patch('gittensor.validator.forward.issue_competitions'),
        patch('gittensor.validator.forward.blend_emission_pools', return_value=[]),
        patch('gittensor.validator.forward.VALIDATOR_WAIT', 0),
    ):
        await forward(mock_self)

        # Verify bulk_store_evaluation was called WITHOUT skip_uids
        call_args = mock_self.bulk_store_evaluation.call_args
        assert call_args is not None
        assert len(call_args[0]) == 1
        assert 'skip_uids' not in call_args[1]
