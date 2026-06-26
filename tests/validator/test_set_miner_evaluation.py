"""Tests for Repository.set_miner_evaluation — the per-(miner, repo) row fan-out."""

from contextlib import contextmanager
from unittest.mock import MagicMock

from gittensor.classes import MinerEvaluation, RepoEvaluation
from gittensor.validator.storage.repository import Repository
from gittensor.validator.utils.load_weights import RepositoryConfig

# Column positions in the BULK_UPSERT_MINER_EVALUATION value tuple.
_REPO_NAME = 3
_TOTAL_SCORE = 6
_IS_ELIGIBLE = 14


def _repo_with_mock_cursor():
    repo = Repository(MagicMock())
    cursor = MagicMock()

    @contextmanager
    def fake_cursor():
        yield cursor

    repo.get_cursor = fake_cursor  # type: ignore[method-assign]
    return repo, cursor


def test_writes_one_row_per_master_repo():
    repo, cursor = _repo_with_mock_cursor()
    master = {f'owner/repo{i}': RepositoryConfig(emission_share=0.1) for i in range(4)}

    evaluation = MinerEvaluation(uid=7, hotkey='hk', github_id='99')
    evaluation.repo_evaluations['owner/repo0'] = RepoEvaluation(
        repository_full_name='owner/repo0', is_eligible=True, credibility=1.0, total_score=42.0
    )

    assert repo.set_miner_evaluation(evaluation, master, commit=False) is True

    rows = cursor.executemany.call_args.args[1]
    assert len(rows) == len(master)
    by_repo = {row[_REPO_NAME]: row for row in rows}
    assert set(by_repo) == set(master)
    # the touched repo carries its RepoEvaluation
    assert by_repo['owner/repo0'][_IS_ELIGIBLE] is True
    assert by_repo['owner/repo0'][_TOTAL_SCORE] == 42.0
    # untouched repos get a zeroed row
    assert by_repo['owner/repo1'][_IS_ELIGIBLE] is False
    assert by_repo['owner/repo1'][_TOTAL_SCORE] == 0.0


def test_empty_master_repositories_writes_nothing():
    repo, cursor = _repo_with_mock_cursor()
    evaluation = MinerEvaluation(uid=7, hotkey='hk', github_id='99')

    assert repo.set_miner_evaluation(evaluation, {}, commit=False) is True
    cursor.executemany.assert_not_called()
