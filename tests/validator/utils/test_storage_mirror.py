"""Verify DatabaseStorage.store_evaluation walks both legacy and mirror PR lists.

Mocks the Repository so we can assert the exact lists passed to
store_pull_requests_bulk without touching a real DB.
"""

from unittest.mock import MagicMock, patch

import pytest

storage_module = pytest.importorskip(
    'gittensor.validator.utils.storage',
    reason='Requires gittensor package',
)
classes = pytest.importorskip('gittensor.classes')
mirror_models = pytest.importorskip('gittensor.utils.mirror.models')
scored_pr_module = pytest.importorskip('gittensor.validator.oss_contributions.mirror.scored_pr')

DatabaseStorage = storage_module.DatabaseStorage
MinerEvaluation = classes.MinerEvaluation
PullRequest = classes.PullRequest
PRState = classes.PRState
MirrorPullRequest = mirror_models.MirrorPullRequest
ScoredMirrorPR = scored_pr_module.ScoredMirrorPR


def _legacy_pr(number: int, state: PRState = PRState.MERGED) -> PullRequest:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return PullRequest(
        number=number,
        repository_full_name='legacy/repo',
        uid=1,
        hotkey='hk',
        github_id='123',
        title=f'legacy {number}',
        author_login='alice',
        merged_at=now if state == PRState.MERGED else None,
        created_at=now,
        pr_state=state,
    )


def _mirror_scored(number: int, state: str = 'MERGED') -> ScoredMirrorPR:
    pr = MirrorPullRequest.from_dict(
        {
            'repo_full_name': 'entrius/gittensor-ui',
            'pr_number': number,
            'title': f'mirror {number}',
            'body': 'b',
            'state': state,
            'author_github_id': '1',
            'author_login': 'a',
            'author_association': 'CONTRIBUTOR',
            'created_at': '2026-04-15T00:00:00Z',
            'closed_at': '2026-04-18T10:00:00Z' if state in ('CLOSED', 'MERGED') else None,
            'merged_at': '2026-04-18T10:00:00Z' if state == 'MERGED' else None,
            'last_edited_at': None,
            'edited_after_merge': False,
            'hours_since_merge': 1.0 if state == 'MERGED' else None,
            'merged_by_login': 'm' if state == 'MERGED' else None,
            'base_ref': 'main',
            'head_ref': 'feature',
            'head_repo_full_name': 'entrius/gittensor-ui',
            'default_branch': 'main',
            'head_sha': 'h',
            'base_sha': 'b',
            'merge_base_sha': 'mb',
            'additions': 1,
            'deletions': 0,
            'commits_count': 1,
            'scoring_data_stored': True,
            'review_summary': {'maintainer_changes_requested_count': 0},
            'labels': [],
            'linked_issues': [],
        }
    )
    return ScoredMirrorPR(pr=pr)


def _make_storage_with_mock_repo():
    """Build a DatabaseStorage with a mock DB connection + repo, bypassing __init__."""
    with patch.object(storage_module, 'create_database_connection', return_value=MagicMock()):
        with patch.object(storage_module, 'Repository') as mock_repo_cls:
            mock_repo = MagicMock()
            mock_repo.set_miner.return_value = 1
            mock_repo.store_pull_requests_bulk.return_value = 0  # actual count irrelevant
            mock_repo.store_issues_bulk.return_value = 0
            mock_repo.store_file_changes_bulk.return_value = 0
            mock_repo.set_miner_evaluation.return_value = True
            mock_repo_cls.return_value = mock_repo
            storage = DatabaseStorage()
            return storage, mock_repo


class TestStoreEvaluationCombinesBothLists:
    def test_merged_lists_concatenated_for_storage(self):
        storage, mock_repo = _make_storage_with_mock_repo()

        eval_ = MinerEvaluation(uid=1, hotkey='hk', github_id='123')
        eval_.merged_pull_requests = [_legacy_pr(1), _legacy_pr(2)]
        eval_.mirror_merged_prs = [_mirror_scored(100), _mirror_scored(101)]

        storage.store_evaluation(eval_)

        # First call to store_pull_requests_bulk is for merged
        merged_call = mock_repo.store_pull_requests_bulk.call_args_list[0]
        merged_arg = merged_call.args[0]
        # 2 legacy + 2 mirror adapted = 4 PullRequest objects
        assert len(merged_arg) == 4
        # All adapted to PullRequest type
        for pr in merged_arg:
            assert isinstance(pr, PullRequest)
        # Legacy PRs come first (order preserved by + concatenation)
        assert merged_arg[0].number == 1
        assert merged_arg[1].number == 2
        # Mirror PRs adapted with correct identity
        assert merged_arg[2].number == 100
        assert merged_arg[2].uid == 1
        assert merged_arg[2].hotkey == 'hk'
        assert merged_arg[2].github_id == '123'
        assert merged_arg[3].number == 101

    def test_open_lists_concatenated(self):
        storage, mock_repo = _make_storage_with_mock_repo()

        eval_ = MinerEvaluation(uid=1, hotkey='hk', github_id='123')
        eval_.open_pull_requests = [_legacy_pr(5, state=PRState.OPEN)]
        eval_.mirror_open_prs = [_mirror_scored(105, state='OPEN')]

        storage.store_evaluation(eval_)

        # Second call (after merged) is open
        open_call = mock_repo.store_pull_requests_bulk.call_args_list[1]
        open_arg = open_call.args[0]
        assert len(open_arg) == 2
        assert open_arg[0].number == 5
        assert open_arg[1].number == 105

    def test_no_mirror_prs_passes_legacy_only(self):
        storage, mock_repo = _make_storage_with_mock_repo()

        eval_ = MinerEvaluation(uid=1, hotkey='hk', github_id='123')
        eval_.merged_pull_requests = [_legacy_pr(1)]
        # mirror_merged_prs left empty

        storage.store_evaluation(eval_)

        merged_arg = mock_repo.store_pull_requests_bulk.call_args_list[0].args[0]
        assert len(merged_arg) == 1
        assert merged_arg[0].number == 1

    def test_no_legacy_prs_passes_mirror_only(self):
        storage, mock_repo = _make_storage_with_mock_repo()

        eval_ = MinerEvaluation(uid=1, hotkey='hk', github_id='123')
        eval_.mirror_merged_prs = [_mirror_scored(100)]
        # merged_pull_requests left empty

        storage.store_evaluation(eval_)

        merged_arg = mock_repo.store_pull_requests_bulk.call_args_list[0].args[0]
        assert len(merged_arg) == 1
        assert merged_arg[0].number == 100
        assert isinstance(merged_arg[0], PullRequest)


def test_cleanup_stale_called_with_commit_false():
    """cleanup_stale_miner_data must be called with commit=False inside the transaction.

    Regression test for #749: cleanup_stale_miner_data was called without
    commit=False, causing its four execute_command calls to commit the
    in-flight transaction prematurely.
    """
    storage, mock_repo = _make_storage_with_mock_repo()
    eval_obj = MinerEvaluation(uid=1, hotkey='hk', github_id='gh1')
    eval_obj.mirror_merged_prs = []
    eval_obj.mirror_open_prs = []
    eval_obj.mirror_closed_prs = []

    with patch(
        'gittensor.validator.oss_contributions.mirror.adapters.mirror_scored_pr_to_legacy_pull_request',
        side_effect=lambda s, *a, **kw: s,
    ):
        storage.store_evaluation(eval_obj)

    mock_repo.cleanup_stale_miner_data.assert_called_once_with(eval_obj, commit=False)


def test_failure_after_cleanup_triggers_rollback():
    """A failure between cleanup and set_miner_evaluation must rollback the entire transaction."""
    storage, mock_repo = _make_storage_with_mock_repo()
    mock_repo.set_miner_evaluation.side_effect = RuntimeError('DB write failed')

    eval_obj = MinerEvaluation(uid=1, hotkey='hk', github_id='gh1')
    eval_obj.mirror_merged_prs = []
    eval_obj.mirror_open_prs = []
    eval_obj.mirror_closed_prs = []

    with patch(
        'gittensor.validator.oss_contributions.mirror.adapters.mirror_scored_pr_to_legacy_pull_request',
        side_effect=lambda s, *a, **kw: s,
    ):
        result = storage.store_evaluation(eval_obj)

    assert result.success is False
    storage.db_connection.rollback.assert_called_once()
    storage.db_connection.commit.assert_not_called()
