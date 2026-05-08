from gittensor.synapses import PatBroadcastSynapse

SECRET = 'ghp_SUPERSECRET12345'


def test_repr_does_not_leak_full_token():
    syn = PatBroadcastSynapse(github_access_token=SECRET)
    assert SECRET not in repr(syn)


def test_str_does_not_leak_full_token():
    syn = PatBroadcastSynapse(github_access_token=SECRET)
    assert SECRET not in str(syn)


def test_repr_keeps_last_four_chars_for_log_correlation():
    syn = PatBroadcastSynapse(github_access_token=SECRET)
    assert '***2345' in repr(syn)


def test_short_token_is_fully_masked():
    syn = PatBroadcastSynapse(github_access_token='abc')
    rep = repr(syn)
    assert 'abc' not in rep
    assert '***' in rep


def test_four_char_token_renders_at_boundary():
    # Pins the >=4 boundary: a 4-char token renders as ***<all 4>. If the
    # threshold ever flips to >4 this test catches the regression instead of
    # the change silently slipping past.
    syn = PatBroadcastSynapse(github_access_token='abcd')
    assert '***abcd' in repr(syn)


def test_empty_token_does_not_crash_repr():
    syn = PatBroadcastSynapse(github_access_token='')
    assert '***' in repr(syn)


def test_response_fields_still_visible_in_repr():
    syn = PatBroadcastSynapse(
        github_access_token=SECRET,
        accepted=True,
        rejection_reason='nope',
    )
    rep = repr(syn)
    assert 'accepted=True' in rep
    assert "rejection_reason='nope'" in rep


def test_model_dump_json_still_includes_full_token():
    syn = PatBroadcastSynapse(github_access_token=SECRET)
    assert SECRET in syn.model_dump_json()


def test_attribute_access_returns_full_token():
    # Validator handlers (pat_handler.handle_pat_broadcast) read the field
    # directly to call validate_github_credentials and save_pat. repr=False
    # must redact only the *display* path, not the attribute path.
    syn = PatBroadcastSynapse(github_access_token=SECRET)
    assert syn.github_access_token == SECRET
