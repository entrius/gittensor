"""Unit tests for load_mirror_miner_prs.

Strategy: build a fake MirrorClient that returns a canned MirrorPullRequestsResponse,
inject it into load_mirror_miner_prs, and assert the resulting bucketing + filtering
on the MirrorMinerEvaluation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

load_module = pytest.importorskip(
    'gittensor.validator.oss_contributions.mirror.load',
    reason='Requires gittensor mirror subpackage',
)
mirror_eval_module = pytest.importorskip('gittensor.validator.oss_contributions.mirror.evaluation')
mirror_models = pytest.importorskip('gittensor.utils.mirror.models')
mirror_client_mod = pytest.importorskip('gittensor.utils.mirror.client')
load_weights = pytest.importorskip('gittensor.validator.utils.load_weights')

load_mirror_miner_prs = load_module.load_mirror_miner_prs
MirrorMinerEvaluation = mirror_eval_module.MirrorMinerEvaluation
MirrorPullRequestsResponse = mirror_models.MirrorPullRequestsResponse
MirrorClient = mirror_client_mod.MirrorClient
MirrorRequestError = mirror_client_mod.MirrorRequestError
RepositoryConfig = load_weights.RepositoryConfig


def _pr_dict(
    pr_number: int,
    repo: str = 'entrius/gittensor-ui',
    state: str = 'MERGED',
    author_association: str = 'CONTRIBUTOR',
    created_at: str = '2026-04-15T00:00:00Z',
    merged_at: str | None = '2026-04-18T10:00:00Z',
    author_login: str = 'bittoby',
    merged_by_login: str | None = 'anderdc',
    approved_count: int = 1,
    base_ref: str = 'main',
    head_ref: str = 'feature/foo',
    default_branch: str = 'main',
):
    return {
        'repo_full_name': repo,
        'pr_number': pr_number,
        'title': f'PR {pr_number}',
        'body': 'b',
        'state': state,
        'author_github_id': '218712309',
        'author_login': author_login,
        'author_association': author_association,
        'created_at': created_at,
        'closed_at': merged_at if state in ('CLOSED', 'MERGED') else None,
        'merged_at': merged_at if state == 'MERGED' else None,
        'last_edited_at': None,
        'edited_after_merge': False,
        'hours_since_merge': 1.0 if state == 'MERGED' else None,
        'merged_by_login': merged_by_login if state == 'MERGED' else None,
        'base_ref': base_ref,
        'head_ref': head_ref,
        'head_repo_full_name': repo,  # same-repo PR; fork cases set explicitly
        'default_branch': default_branch,
        'head_sha': 'h',
        'base_sha': 'b',
        'merge_base_sha': 'mb',
        'additions': 1,
        'deletions': 0,
        'commits_count': 1,
        'scoring_data_stored': True,
        'review_summary': {
            'maintainer_changes_requested_count': 0,
            'changes_requested_count': 0,
            'approved_count': approved_count,
            'commented_count': 0,
        },
        'labels': [],
        'linked_issues': [],
    }


def _build_response(prs: list) -> MirrorPullRequestsResponse:
    return MirrorPullRequestsResponse.from_dict(
        {
            'github_id': '218712309',
            'since': '2026-03-15T00:00:00Z',
            'generated_at': '2026-04-21T00:00:00Z',
            'pull_requests': prs,
        }
    )


def _mirror_repos(*names: str) -> dict:
    return {name: RepositoryConfig(weight=0.5, mirror_enabled=True) for name in names}


def _eval(github_id: str | None = '218712309') -> MirrorMinerEvaluation:
    return MirrorMinerEvaluation(uid=1, hotkey='hk', github_id=github_id)


# ============================================================================
# Bucketing
# ============================================================================


class TestBucketing:
    def test_buckets_by_state(self):
        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                _pr_dict(1, state='MERGED'),
                _pr_dict(2, state='OPEN', merged_at=None),
                _pr_dict(3, state='CLOSED', merged_at=None),
            ]
        )
        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client)

        assert len(eval_.merged_prs) == 1
        assert eval_.merged_prs[0].pr.pr_number == 1
        assert len(eval_.open_prs) == 1
        assert eval_.open_prs[0].pr.pr_number == 2
        assert len(eval_.closed_prs) == 1
        assert eval_.closed_prs[0].pr.pr_number == 3

    def test_unknown_state_skipped_with_warning(self):
        # State not OPEN/CLOSED/MERGED — shouldn't crash, just skip
        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                _pr_dict(1, state='MERGED'),
                _pr_dict(2, state='WEIRD'),
            ]
        )
        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client)

        assert len(eval_.merged_prs) == 1
        assert len(eval_.open_prs) == 0


# ============================================================================
# Repo filtering (mirror returns all tracked repos; filter to mirror-enabled subset)
# ============================================================================


class TestRepoFiltering:
    def test_repo_not_in_config_dropped(self):
        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                _pr_dict(1, repo='entrius/gittensor-ui'),
                _pr_dict(2, repo='entrius/some-untracked-repo'),
            ]
        )
        eval_ = _eval()
        # Only one repo enabled in config — the second PR should be dropped
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client)

        assert len(eval_.merged_prs) == 1
        assert eval_.merged_prs[0].pr.repo_full_name == 'entrius/gittensor-ui'


# ============================================================================
# Maintainer skip (legacy parity)
# ============================================================================


class TestMaintainerSkip:
    @pytest.mark.parametrize('association', ['OWNER', 'MEMBER', 'COLLABORATOR'])
    def test_maintainer_authors_dropped(self, association, monkeypatch):
        monkeypatch.delenv('DEV_MODE', raising=False)
        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                _pr_dict(1, author_association=association),
                _pr_dict(2, author_association='CONTRIBUTOR'),
            ]
        )
        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client)

        assert len(eval_.merged_prs) == 1
        assert eval_.merged_prs[0].pr.pr_number == 2

    def test_dev_mode_bypasses_maintainer_skip(self, monkeypatch):
        monkeypatch.setenv('DEV_MODE', '1')
        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                _pr_dict(1, author_association='OWNER'),
            ]
        )
        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client)

        assert len(eval_.merged_prs) == 1


# ============================================================================
# Inactive repo + stale closed PR
# ============================================================================


class TestInactiveRepo:
    def test_pr_created_after_inactive_at_dropped(self):
        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                # Created on 2026-04-15, repo became inactive on 2026-04-10 → drop
                _pr_dict(1, created_at='2026-04-15T00:00:00Z'),
                # Created on 2026-04-05 (before inactive_at) → keep
                _pr_dict(2, created_at='2026-04-05T00:00:00Z'),
            ]
        )
        repos = {
            'entrius/gittensor-ui': RepositoryConfig(
                weight=0.5,
                mirror_enabled=True,
                inactive_at='2026-04-10T00:00:00Z',
            ),
        }
        eval_ = _eval()
        load_mirror_miner_prs(eval_, repos, client=client)

        assert len(eval_.merged_prs) == 1
        assert eval_.merged_prs[0].pr.pr_number == 2


class TestStaleClosedPR:
    def test_closed_pr_created_before_lookback_dropped(self):
        # Lookback is 35 days before "now"; a CLOSED PR created 50 days ago should drop
        old = (datetime.now(timezone.utc) - timedelta(days=50)).isoformat().replace('+00:00', 'Z')
        recent = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                _pr_dict(1, state='CLOSED', merged_at=None, created_at=old),
                _pr_dict(2, state='CLOSED', merged_at=None, created_at=recent),
            ]
        )
        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client)

        assert len(eval_.closed_prs) == 1
        assert eval_.closed_prs[0].pr.pr_number == 2


# ============================================================================
# Error paths
# ============================================================================


class TestEligibilityGateAtLoadTime:
    """Legacy parity: should_skip_merged_pr gates MERGED PRs at load (pre-append),
    not at score time — otherwise rejected PRs inflate check_eligibility's
    merged_count and distort credibility calculation."""

    def test_self_merge_without_approval_not_added(self, monkeypatch):
        monkeypatch.delenv('DEV_MODE', raising=False)
        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                _pr_dict(1, author_login='alice', merged_by_login='alice', approved_count=0),
                _pr_dict(2),  # clean
            ]
        )
        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client)

        # Rejected PR never enters the merged list (matches legacy behavior)
        assert len(eval_.merged_prs) == 1
        assert eval_.merged_prs[0].pr.pr_number == 2

    def test_base_ref_mismatch_not_added(self):
        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                _pr_dict(1, base_ref='random-branch', default_branch='main'),
                _pr_dict(2),  # base_ref=main matches default
            ]
        )
        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client)

        assert len(eval_.merged_prs) == 1
        assert eval_.merged_prs[0].pr.pr_number == 2

    def test_merged_pr_missing_merged_at_not_added(self):
        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                _pr_dict(1, merged_at=None),  # MERGED state but no merged_at — data corruption
                _pr_dict(2),
            ]
        )
        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client)

        assert len(eval_.merged_prs) == 1
        assert eval_.merged_prs[0].pr.pr_number == 2


class TestErrorPaths:
    def test_no_github_id_short_circuits(self):
        client = Mock()
        eval_ = _eval(github_id=None)
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client)
        client.get_miner_pulls.assert_not_called()
        assert eval_.fetch_failed is False

    def test_no_mirror_repos_short_circuits(self):
        client = Mock()
        eval_ = _eval()
        load_mirror_miner_prs(eval_, {}, client=client)
        client.get_miner_pulls.assert_not_called()

    def test_mirror_request_error_sets_fetch_failed(self):
        client = Mock()
        client.get_miner_pulls.side_effect = MirrorRequestError('boom')
        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client)
        assert eval_.fetch_failed is True
        assert eval_.merged_prs == []

    def test_malformed_2xx_json_sets_fetch_failed(self):
        response = Mock(status_code=200, text='<html>bad gateway</html>')
        response.json.side_effect = ValueError('Expecting value')
        session = Mock()
        session.get.return_value = response
        client = MirrorClient(session=session, max_attempts=1)

        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client)

        assert eval_.fetch_failed is True
        assert eval_.merged_prs == []

    def test_per_pr_exception_does_not_abort_loop(self, monkeypatch):
        """A bad PR shouldn't crash the whole load — log warning and continue."""
        from gittensor.validator.oss_contributions.mirror import load as load_mod

        call_count = {'n': 0}
        original = load_mod._maybe_add_pr

        def flaky(eval_, pr, repos, lookback, registration_cutoff=None):
            call_count['n'] += 1
            if call_count['n'] == 1:
                raise RuntimeError('synthetic failure on first PR')
            original(eval_, pr, repos, lookback, registration_cutoff=registration_cutoff)

        monkeypatch.setattr(load_mod, '_maybe_add_pr', flaky)

        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                _pr_dict(1, state='MERGED'),
                _pr_dict(2, state='MERGED'),
            ]
        )
        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client)
        # First PR raised → skipped; second PR still added
        assert len(eval_.merged_prs) == 1
        assert eval_.merged_prs[0].pr.pr_number == 2
        assert eval_.fetch_failed is False  # per-PR errors don't poison the whole batch


class TestRegistrationCutoffGate:
    def test_merged_before_cutoff_skipped(self):
        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                _pr_dict(1, state='MERGED', merged_at='2026-04-10T00:00:00Z'),
                _pr_dict(2, state='MERGED', merged_at='2026-04-25T00:00:00Z'),
            ]
        )
        cutoff = datetime(2026, 4, 20, tzinfo=timezone.utc)
        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client, registration_cutoff=cutoff)

        assert len(eval_.merged_prs) == 1
        assert eval_.merged_prs[0].pr.pr_number == 2

    def test_merged_at_cutoff_kept(self):
        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                _pr_dict(1, state='MERGED', merged_at='2026-04-10T00:00:00Z'),
            ]
        )
        cutoff = datetime(2026, 4, 10, tzinfo=timezone.utc)
        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client, registration_cutoff=cutoff)

        assert len(eval_.merged_prs) == 1
        assert eval_.merged_prs[0].pr.pr_number == 1

    def test_closed_before_cutoff_skipped(self):
        recent_create = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                _pr_dict(
                    1,
                    state='CLOSED',
                    merged_at=None,
                    created_at=recent_create,
                ),
            ]
        )
        # Override closed_at to before cutoff (mirror dict sets closed_at=merged_at;
        # for state=CLOSED with merged_at=None, closed_at is None — so set it explicitly)
        prs = client.get_miner_pulls.return_value.pull_requests
        prs[0].closed_at = datetime(2026, 4, 10, tzinfo=timezone.utc)

        cutoff = datetime(2026, 4, 20, tzinfo=timezone.utc)
        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client, registration_cutoff=cutoff)

        assert len(eval_.closed_prs) == 0

    def test_no_cutoff_does_not_skip(self):
        client = Mock()
        client.get_miner_pulls.return_value = _build_response(
            [
                _pr_dict(1, state='MERGED', merged_at='2026-04-10T00:00:00Z'),
            ]
        )
        eval_ = _eval()
        load_mirror_miner_prs(eval_, _mirror_repos('entrius/gittensor-ui'), client=client)

        assert len(eval_.merged_prs) == 1
