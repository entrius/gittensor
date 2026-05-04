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
