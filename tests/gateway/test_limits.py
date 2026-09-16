# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The limits alone: they refuse, never clamp or inject, and never touch message shapes."""

import pytest

from gittensor.gateway.limits import RequestRefused, enforce_openai_limits, remote_media


def test_the_token_fields_go_on_as_sent_and_nothing_is_injected():
    # No cap of ours (Kimbo 9/16): the runtime's own limit applies. 9/16 soak: the gateway still clamped at 4096.
    big = {'model': 'm', 'max_tokens': 20_000}
    assert enforce_openai_limits(big, '/v1/completions') is None and big == {'model': 'm', 'max_tokens': 20_000}
    other = {'model': 'm', 'max_completion_tokens': 5_000}
    enforce_openai_limits(other, '/v1/chat/completions')
    assert other == {'model': 'm', 'max_completion_tokens': 5_000}
    none = {'model': 'm', 'messages': []}
    enforce_openai_limits(none, '/v1/chat/completions')
    assert none == {'model': 'm', 'messages': []}  # no max_tokens key appears


@pytest.mark.parametrize(
    'content, expected',
    [
        ('plain string', ''),
        ([{'type': 'text', 'text': 'hi'}], ''),
        ([{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}], ''),
        ([{'type': 'image_url', 'image_url': {'url': 'https://x/cat.png'}}], 'image_url'),
        ([{'type': 'image_url', 'image_url': 'http://x/cat.png'}], 'image_url'),
        ([{'type': 'video_url', 'video_url': {'url': 'file:///etc/passwd'}}], 'video_url'),
        ([{'type': 'image_url', 'image_url': {'url': 42}}], ''),  # malformed: the runtime's 400, not ours
    ],
)
def test_remote_media(content, expected):
    assert remote_media([{'role': 'user', 'content': content}]) == expected


def test_limits_leave_unusual_shapes_to_the_runtime():
    body = {'model': 'm', 'messages': 'not even a list', 'max_tokens': 10, 'tools': 'whatever', 'n': 1}
    enforce_openai_limits(body, '/v1/chat/completions')
    assert body == {'model': 'm', 'messages': 'not even a list', 'max_tokens': 10, 'tools': 'whatever', 'n': 1}


@pytest.mark.parametrize('bad', [0, -1, 1.5, True, '10'])
def test_a_bad_token_limit_is_refused(bad):
    with pytest.raises(RequestRefused, match='max_tokens must be a positive integer'):
        enforce_openai_limits({'max_tokens': bad}, '/v1/completions')
