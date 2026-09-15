# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The registry: bless signs an entry that reads back verified; a tampered, re-signed-elsewhere, wrong-namespace or
non-canonical entry is refused on read; deployment settings are separate and round-trip."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from gittensor.agent.config import DEV_KEY_MARKER
from gittensor.controller.manifest import ManifestError
from gittensor.controller.registry import (
    DeploymentStore,
    Registry,
    RegistryError,
    canonical_json,
    load_release_pubkey,
    make_entry,
    sign_bytes,
)

FIXTURE = Path(__file__).parent / 'fixtures' / 'manifest_27b.yaml'
IMAGE = 'entrius/sparkinfer:19ef39ec2@sha256:' + 'ab' * 32


def keypair(tmp_path: Path, name: str, comment: str = 'test-release') -> tuple[Path, str]:
    key = tmp_path / name
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', comment, '-f', str(key)], check=True)
    return key, (tmp_path / f'{name}.pub').read_text().strip()


@pytest.fixture
def release(tmp_path):
    return keypair(tmp_path, 'release')


def blessed(tmp_path, release):
    key, pub = release
    registry = Registry(tmp_path / 'registry', pub)
    verified = make_entry(yaml.safe_load(FIXTURE.read_text()), IMAGE, now=1_757_000_000)
    registry.write(verified, sign_bytes(verified.entry.canonical_bytes(), key))
    return registry, verified


def test_sign_verify_round_trip(tmp_path, release):
    registry, verified = blessed(tmp_path, release)
    assert registry.ids() == ['qwen3.8-27b-nvfp4@1']
    read = registry.read('qwen3.8-27b-nvfp4@1')
    assert read.entry == verified.entry and read.manifest.image == IMAGE and read.entry.manifest['image'] == IMAGE
    payload = (tmp_path / 'registry' / 'qwen3.8-27b-nvfp4@1.json').read_bytes()
    assert payload == canonical_json(read.entry.as_dict()) and b' ' not in payload.split(b'"manifest"')[0]


def test_a_tampered_entry_is_refused(tmp_path, release):
    registry, _ = blessed(tmp_path, release)
    path = tmp_path / 'registry' / 'qwen3.8-27b-nvfp4@1.json'
    path.write_bytes(path.read_bytes().replace(b'abab', b'cdcd', 1))  # repoint the image digest
    with pytest.raises(RegistryError, match='does not verify'):
        registry.read('qwen3.8-27b-nvfp4@1')


def test_signed_by_another_key_or_for_another_purpose_is_refused(tmp_path, release):
    key, pub = release
    other_key, _ = keypair(tmp_path, 'other')
    verified = make_entry(yaml.safe_load(FIXTURE.read_text()), IMAGE)
    registry = Registry(tmp_path / 'registry', pub)
    with pytest.raises(RegistryError, match='does not verify'):
        registry.write(verified, sign_bytes(verified.entry.canonical_bytes(), other_key))
    channel_sig = sign_bytes(verified.entry.canonical_bytes(), key, namespace='gt-agent-channel')
    with pytest.raises(RegistryError, match='does not verify'):
        registry.write(verified, channel_sig)
    assert registry.ids() == []


def test_non_canonical_or_mismatched_entries_are_refused(tmp_path, release):
    key, pub = release
    registry, verified = blessed(tmp_path, release)
    path, sig = registry.paths('qwen3.8-27b-nvfp4@1')
    # validly signed, but pretty-printed: not the canonical bytes
    pretty = json.dumps(verified.entry.as_dict(), indent=1).encode()
    path.write_bytes(pretty)
    sig.write_bytes(sign_bytes(pretty, key))
    with pytest.raises(RegistryError, match='canonical'):
        registry.read('qwen3.8-27b-nvfp4@1')
    # validly signed and canonical, but filed under another version
    other, other_sig = registry.paths('qwen3.8-27b-nvfp4@2')
    other.write_bytes(verified.entry.canonical_bytes())
    other_sig.write_bytes(sign_bytes(verified.entry.canonical_bytes(), key))
    with pytest.raises(RegistryError, match='disagree'):
        registry.read('qwen3.8-27b-nvfp4@2')
    with pytest.raises(RegistryError, match='No such file'):
        Registry(tmp_path / 'nowhere', pub).read('qwen3.8-27b-nvfp4@1')
    with pytest.raises(RegistryError, match='not an entry id'):
        registry.read('../boxes')


def test_bless_refuses_a_placeholder_digest_or_an_unpinned_image():
    document = yaml.safe_load(FIXTURE.read_text())
    with pytest.raises(ManifestError, match='placeholder'):
        make_entry(document, 'entrius/sparkinfer@sha256:' + '0' * 64)
    with pytest.raises(ManifestError, match='image'):
        make_entry(document, 'entrius/sparkinfer:latest')


def test_dev_release_key_needs_the_override(tmp_path):
    _, pub = keypair(tmp_path, 'dev', comment=f'gt-release-dev {DEV_KEY_MARKER}')
    with pytest.raises(RegistryError, match='--allow-dev-keys'):
        load_release_pubkey(tmp_path / 'dev.pub')
    assert load_release_pubkey(tmp_path / 'dev.pub', allow_dev_keys=True) == pub
    assert load_release_pubkey(None).startswith('ssh-ed25519 ')


def test_deployments_are_separate_and_round_trip(tmp_path):
    store = DeploymentStore(tmp_path / 'deployments.json')
    assert store.get('x@1').desired == 0
    store.set('x@1', enabled=True, replicas=2)
    store.set('x@1', enabled=False)
    reopened = DeploymentStore(tmp_path / 'deployments.json')
    assert vars(reopened.get('x@1')) == {'enabled': False, 'replicas': 2} and reopened.get('x@1').desired == 0
    with pytest.raises(ValueError):
        store.set('x@1', replicas=-1)
