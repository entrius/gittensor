# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Tests for tracked-repo PAT validation."""

import importlib
import sys
from types import ModuleType, SimpleNamespace


def _install_validation_stubs(monkeypatch):
    fake_constants = ModuleType('gittensor.constants')
    fake_constants.BASE_GITHUB_API_URL = 'https://api.github.com'
    fake_constants.GITHUB_HTTP_TIMEOUT_SECONDS = 15
    monkeypatch.setitem(sys.modules, 'gittensor.constants', fake_constants)

    fake_github_api_tools = ModuleType('gittensor.utils.github_api_tools')
    fake_github_api_tools.get_github_id = lambda _pat: '123'
    monkeypatch.setitem(sys.modules, 'gittensor.utils.github_api_tools', fake_github_api_tools)

    fake_load_weights = ModuleType('gittensor.validator.utils.load_weights')
    fake_load_weights.load_master_repo_weights = lambda: {'owner/repo': SimpleNamespace(weight=1.0)}
    monkeypatch.setitem(sys.modules, 'gittensor.validator.utils.load_weights', fake_load_weights)

    monkeypatch.delitem(sys.modules, 'gittensor.validator.utils.github_validation', raising=False)


def test_validate_github_repo_access_queries_tracked_repo(monkeypatch):
    _install_validation_stubs(monkeypatch)
    github_validation = importlib.import_module('gittensor.validator.utils.github_validation')

    captured = {}

    def fake_post(url, json, headers, timeout):
        captured['url'] = url
        captured['json'] = json
        captured['headers'] = headers
        captured['timeout'] = timeout
        return SimpleNamespace(status_code=200, json=lambda: {'data': {'repository': {'id': 'repo123'}}})

    monkeypatch.setattr(github_validation.requests, 'post', fake_post)

    assert github_validation.validate_github_repo_access('ghp_token') is None
    assert captured['json']['variables'] == {'owner': 'owner', 'name': 'repo'}
    assert 'repository(owner: $owner, name: $name)' in captured['json']['query']


def test_validate_github_repo_access_rejects_missing_repository(monkeypatch):
    _install_validation_stubs(monkeypatch)
    github_validation = importlib.import_module('gittensor.validator.utils.github_validation')

    monkeypatch.setattr(
        github_validation.requests,
        'post',
        lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: {'data': {'repository': None}}),
    )

    error = github_validation.validate_github_repo_access('ghp_token')
    assert 'could not access tracked repo owner/repo' in error
