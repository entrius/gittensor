# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Root pytest configuration shared across all test directories.
"""

import pytest
import requests


class _ForwardingSession:
    """Session-like proxy delegating .get/.post to requests.get/post at call time.

    Preserves compatibility with tests that @patch requests.get/post while production uses requests.Session via get_session().
    """

    def __init__(self):
        self.headers = {}

    def get(self, *args, **kwargs):
        if 'headers' not in kwargs:
            kwargs['headers'] = dict(self.headers)
        return requests.get(*args, **kwargs)

    def post(self, *args, **kwargs):
        if 'headers' not in kwargs:
            kwargs['headers'] = dict(self.headers)
        return requests.post(*args, **kwargs)

    def close(self):
        """Mirror requests.Session.close so callers can release the session."""


@pytest.fixture(autouse=True)
def _forward_github_sessions(monkeypatch):
    """Replace get_session with a forwarding proxy so @patch(requests.get/post) still works."""
    try:
        from gittensor.utils import github_api_tools
    except ImportError:
        yield
        return

    def _forwarding_get_session(token):
        session = _ForwardingSession()
        headers = github_api_tools.make_headers(token) if token else github_api_tools.make_anonymous_headers()
        session.headers.update(headers)
        return session

    monkeypatch.setattr(github_api_tools, 'get_session', _forwarding_get_session)
    yield
