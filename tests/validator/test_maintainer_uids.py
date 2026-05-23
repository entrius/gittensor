# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Unit tests for _build_maintainer_uids_by_repo — the forward-pass helper that
resolves a repo's mirror maintainer roster to registered miner UIDs for the
maintainer_cut carve-out."""

from gittensor.classes import MinerEvaluation
from gittensor.utils.mirror.client import MirrorRequestError
from gittensor.utils.mirror.models import MirrorMaintainer, MirrorRepoMaintainersResponse
from gittensor.validator import forward
from gittensor.validator.forward import _build_maintainer_uids_by_repo
from gittensor.validator.utils.load_weights import RepositoryConfig


class _StubMirrorClient:
    """Context-manager stand-in for MirrorClient with canned per-repo responses."""

    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def __enter__(self) -> '_StubMirrorClient':
        return self

    def __exit__(self, *args) -> None:
        return None

    def get_repo_maintainers(self, repo_full_name: str) -> MirrorRepoMaintainersResponse:
        self.calls.append(repo_full_name)
        result = self._responses[repo_full_name]
        if isinstance(result, Exception):
            raise result
        return result


def _install_stub(monkeypatch, responses) -> _StubMirrorClient:
    stub = _StubMirrorClient(responses)
    monkeypatch.setattr(forward, 'MirrorClient', lambda: stub)
    return stub


def _evaluation(uid: int, github_id: str) -> MinerEvaluation:
    return MinerEvaluation(uid=uid, hotkey=f'hk-{uid}', github_id=github_id)


def _response(repo: str, *github_ids: int) -> MirrorRepoMaintainersResponse:
    return MirrorRepoMaintainersResponse(
        repo_full_name=repo,
        generated_at=None,
        maintainers=[MirrorMaintainer(github_id=str(gid), login=f'u{gid}', association='MEMBER') for gid in github_ids],
    )


def _cut_repo(maintainer_cut: float = 0.5) -> RepositoryConfig:
    return RepositoryConfig(emission_share=0.1, maintainer_cut=maintainer_cut)


def test_resolves_github_ids_to_registered_uids(monkeypatch):
    _install_stub(monkeypatch, {'r/one': _response('r/one', 100, 200)})
    evaluations = {1: _evaluation(1, '100'), 2: _evaluation(2, '200'), 3: _evaluation(3, '300')}

    result = _build_maintainer_uids_by_repo(evaluations, {'r/one': _cut_repo()}, {1, 2, 3})

    assert result == {'r/one': [1, 2]}


def test_unregistered_and_zero_github_id_maintainers_excluded(monkeypatch):
    # gh 100 is a registered miner; gh 999 has no miner; uid 2 broadcast no PAT (github_id '0').
    _install_stub(monkeypatch, {'r/one': _response('r/one', 100, 999)})
    evaluations = {1: _evaluation(1, '100'), 2: _evaluation(2, '0')}

    result = _build_maintainer_uids_by_repo(evaluations, {'r/one': _cut_repo()}, {1, 2})

    assert result == {'r/one': [1]}


def test_maintainer_uid_not_in_miner_uids_excluded(monkeypatch):
    _install_stub(monkeypatch, {'r/one': _response('r/one', 100, 200)})
    evaluations = {1: _evaluation(1, '100'), 2: _evaluation(2, '200')}

    result = _build_maintainer_uids_by_repo(evaluations, {'r/one': _cut_repo()}, {1})

    assert result == {'r/one': [1]}


def test_only_queries_repos_with_positive_cut(monkeypatch):
    stub = _install_stub(monkeypatch, {'r/cut': _response('r/cut', 100)})
    evaluations = {1: _evaluation(1, '100')}
    repos = {'r/cut': _cut_repo(0.5), 'r/no-cut': _cut_repo(0.0)}

    result = _build_maintainer_uids_by_repo(evaluations, repos, {1})

    assert stub.calls == ['r/cut']
    assert result == {'r/cut': [1]}


def test_mirror_failure_omits_repo(monkeypatch):
    _install_stub(monkeypatch, {'r/one': MirrorRequestError('mirror down')})
    evaluations = {1: _evaluation(1, '100')}

    result = _build_maintainer_uids_by_repo(evaluations, {'r/one': _cut_repo()}, {1})

    assert result == {}


def test_empty_maintainers_omits_repo(monkeypatch):
    _install_stub(monkeypatch, {'r/one': _response('r/one')})
    evaluations = {1: _evaluation(1, '100')}

    result = _build_maintainer_uids_by_repo(evaluations, {'r/one': _cut_repo()}, {1})

    assert result == {}


def test_duplicate_github_ids_dedup(monkeypatch):
    _install_stub(monkeypatch, {'r/one': _response('r/one', 100, 100)})
    evaluations = {1: _evaluation(1, '100')}

    result = _build_maintainer_uids_by_repo(evaluations, {'r/one': _cut_repo()}, {1})

    assert result == {'r/one': [1]}


def test_penalized_miner_excluded_from_maintainer_uids(monkeypatch):
    # A miner with failed_reason set (e.g. duplicate-github penalty) must not
    # collect the maintainer carve-out — github_id is preserved on the eval for
    # attribution, but downstream score-skipping paths must honor failed_reason.
    _install_stub(monkeypatch, {'r/one': _response('r/one', 100, 200)})
    penalized = _evaluation(1, '100')
    penalized.failed_reason = 'duplicate_github_account'
    evaluations = {1: penalized, 2: _evaluation(2, '200')}

    result = _build_maintainer_uids_by_repo(evaluations, {'r/one': _cut_repo()}, {1, 2})

    assert result == {'r/one': [2]}


def test_all_maintainers_penalized_omits_repo(monkeypatch):
    # If every registered maintainer-miner is penalized, the repo drops out entirely
    # so blend_emission_pools skips the carve-out and scores the slice normally.
    _install_stub(monkeypatch, {'r/one': _response('r/one', 100)})
    penalized = _evaluation(1, '100')
    penalized.failed_reason = 'duplicate_github_account'
    evaluations = {1: penalized}

    result = _build_maintainer_uids_by_repo(evaluations, {'r/one': _cut_repo()}, {1})

    assert result == {}


def test_no_cut_repos_returns_empty_without_client(monkeypatch):
    def _boom() -> None:
        raise AssertionError('MirrorClient should not be constructed when no repo has a cut')

    monkeypatch.setattr(forward, 'MirrorClient', _boom)
    evaluations = {1: _evaluation(1, '100')}

    result = _build_maintainer_uids_by_repo(evaluations, {'r/one': _cut_repo(0.0)}, {1})

    assert result == {}
