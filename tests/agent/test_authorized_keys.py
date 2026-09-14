# The MIT License (MIT)
# Copyright © 2025 Entrius

"""authorized_keys editing: normalization, idempotent append/remove, preservation of foreign lines, permissions."""

import os

import pytest

from gittensor.agent.authorized_keys import (
    InvalidPublicKey,
    install_key,
    normalize_pubkey,
    remove_key,
    tagged_keys,
)
from tests.agent.conftest import OTHER_PUBKEY, TEST_PUBKEY, TEST_PUBKEY_BODY


class TestNormalize:
    def test_drops_comment(self):
        assert normalize_pubkey(TEST_PUBKEY) == TEST_PUBKEY_BODY

    def test_tolerates_whitespace(self):
        assert normalize_pubkey(f'  {TEST_PUBKEY}\n') == TEST_PUBKEY_BODY

    @pytest.mark.parametrize(
        'bad',
        [
            '',
            'ssh-ed25519',
            'not a key at all',
            'ssh-dss AAAAB3NzaC1kc3MAAACBAP',  # unsupported type
            'ssh-ed25519 !!!not-base64!!!',
            'ssh-rsa ' + TEST_PUBKEY.split()[1],  # declared rsa, blob says ed25519
            'ssh-ed25519 AAAA',  # too short
        ],
    )
    def test_rejects_garbage(self, bad):
        with pytest.raises(InvalidPublicKey):
            normalize_pubkey(bad)

    def test_rejects_oversized(self):
        with pytest.raises(InvalidPublicKey):
            normalize_pubkey('ssh-ed25519 ' + 'A' * 5000)


class TestInstallRemove:
    def test_install_creates_file_with_tag_and_perms(self, tmp_path):
        path = tmp_path / 'ssh' / 'authorized_keys'
        assert install_key(path, TEST_PUBKEY) is True
        assert path.read_text() == f'{TEST_PUBKEY_BODY} gittensor-controller\n'
        assert oct(path.stat().st_mode & 0o777) == '0o600'
        assert oct(path.parent.stat().st_mode & 0o777) == '0o700'

    def test_install_is_idempotent(self, tmp_path):
        path = tmp_path / 'authorized_keys'
        assert install_key(path, TEST_PUBKEY) is True
        assert install_key(path, TEST_PUBKEY) is False
        assert install_key(path, TEST_PUBKEY_BODY + ' some-other-comment') is False
        assert path.read_text().count(TEST_PUBKEY_BODY) == 1

    def test_remove_is_idempotent_and_preserves_others(self, tmp_path):
        path = tmp_path / 'authorized_keys'
        foreign = f'no-pty,command="/bin/true" {OTHER_PUBKEY}'
        path.write_text(f'# operator comment\n{foreign}\n')
        install_key(path, TEST_PUBKEY)
        assert remove_key(path, TEST_PUBKEY) is True
        assert remove_key(path, TEST_PUBKEY) is False
        assert path.read_text() == f'# operator comment\n{foreign}\n'

    def test_remove_matches_by_key_body_not_comment(self, tmp_path):
        path = tmp_path / 'authorized_keys'
        install_key(path, TEST_PUBKEY)
        assert remove_key(path, TEST_PUBKEY_BODY + ' renamed') is True
        assert path.read_text() == ''

    def test_remove_from_missing_file_is_false(self, tmp_path):
        assert remove_key(tmp_path / 'nope', TEST_PUBKEY) is False

    def test_tagged_keys_lists_only_ours(self, tmp_path):
        path = tmp_path / 'authorized_keys'
        path.write_text(f'{OTHER_PUBKEY}\n')
        install_key(path, TEST_PUBKEY)
        assert tagged_keys(path) == [TEST_PUBKEY_BODY]

    def test_write_is_atomic_no_temp_left_behind(self, tmp_path):
        path = tmp_path / 'authorized_keys'
        install_key(path, TEST_PUBKEY)
        remove_key(path, TEST_PUBKEY)
        assert os.listdir(tmp_path) == ['authorized_keys']
