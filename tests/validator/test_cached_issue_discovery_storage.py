from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

forward_module = pytest.importorskip('gittensor.validator.forward')
storage_module = pytest.importorskip('gittensor.validator.utils.storage')
classes = pytest.importorskip('gittensor.classes')
load_weights = pytest.importorskip('gittensor.validator.utils.load_weights')
validator_module = pytest.importorskip('neurons.validator')

DatabaseStorage = storage_module.DatabaseStorage
StorageResult = storage_module.StorageResult
MinerEvaluation = classes.MinerEvaluation
RepositoryConfig = load_weights.RepositoryConfig
TokenConfig = load_weights.TokenConfig
Validator = validator_module.Validator


def test_issue_refreshed_cached_uids_are_evaluation_only_storage_uids():
    evaluation_only_uids = forward_module.issue_refreshed_cached_uids(
        cached_uids={1, 2, 3},
        issue_refreshed_uids={2, 4},
    )

    assert evaluation_only_uids == {2}


def test_issue_discovery_returns_refreshed_uids_from_mirror_scan(monkeypatch):
    async def fake_run_mirror_issue_discovery(*args, **kwargs):
        return {7}

    monkeypatch.setattr(forward_module, 'run_mirror_issue_discovery', fake_run_mirror_issue_discovery)

    evaluations = {7: MinerEvaluation(uid=7, hotkey='hk7', github_id='777')}
    repos = {'entrius/gittensor': RepositoryConfig(weight=1.0, mirror_enabled=True)}

    rewards, refreshed_uids = asyncio.run(
        forward_module.issue_discovery(evaluations, repos, {}, TokenConfig(), miner_uids={7})
    )

    assert rewards.tolist() == [0.0]
    assert refreshed_uids == {7}


def test_bulk_store_refreshes_cached_uid_with_evaluation_only_storage():
    validator = MagicMock()
    validator.db_storage.store_evaluation.return_value = StorageResult(True, [], {})
    validator.db_storage.store_miner_evaluation.return_value = StorageResult(True, [], {})

    cached_eval = MinerEvaluation(uid=1, hotkey='hk1', github_id='111')
    fresh_eval = MinerEvaluation(uid=2, hotkey='hk2', github_id='222')

    asyncio.run(
        Validator.bulk_store_evaluation(
            cast(Validator, validator),
            {1: cached_eval, 2: fresh_eval},
            skip_uids={1},
            evaluation_only_uids={1},
        )
    )

    validator.db_storage.store_miner_evaluation.assert_called_once_with(cached_eval)
    validator.db_storage.store_evaluation.assert_called_once_with(fresh_eval)


def test_store_miner_evaluation_skips_pr_issue_and_file_payloads():
    with patch.object(storage_module, 'create_database_connection', return_value=MagicMock()):
        with patch.object(storage_module, 'Repository') as mock_repo_cls:
            mock_repo = MagicMock()
            mock_repo.set_miner.return_value = 1
            mock_repo.set_miner_evaluation.return_value = True
            mock_repo_cls.return_value = mock_repo

            storage = DatabaseStorage()
            evaluation = MinerEvaluation(uid=1, hotkey='hk1', github_id='111')

            result = storage.store_miner_evaluation(evaluation)

    assert result.success is True
    mock_repo.set_miner.assert_called_once()
    mock_repo.cleanup_stale_miner_data.assert_called_once_with(evaluation, commit=False)
    mock_repo.set_miner_evaluation.assert_called_once_with(evaluation, commit=False)
    mock_repo.store_pull_requests_bulk.assert_not_called()
    mock_repo.store_issues_bulk.assert_not_called()
    mock_repo.store_file_changes_bulk.assert_not_called()
