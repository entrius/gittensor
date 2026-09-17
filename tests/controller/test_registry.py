# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The registry: bless signs an entry that reads back verified; a tampered, re-signed-elsewhere, wrong-namespace or
non-canonical entry is refused on read; deployment settings are separate and round-trip."""

import json
import subprocess
from pathlib import Path
from typing import Any

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
QUALIFIED = {
    'at': 1_757_900_000,
    'box': 'local',
    'driver': '580.65.06',
    'load_s': 16.5,
    'health_ok': True,
    'canary_ok': True,
    'vram_gb': 25.1,
    'decode_tps_single': 99.0,
    'notes': 'page cache warm',
}


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


def test_the_qualified_block_is_signed_beside_the_manifest_and_entries_without_it_still_verify(tmp_path, release):
    key, pub = release
    registry = Registry(tmp_path / 'registry', pub)
    verified = make_entry(yaml.safe_load(FIXTURE.read_text()), IMAGE, now=1_757_000_000, qualified=QUALIFIED)
    registry.write(verified, sign_bytes(verified.entry.canonical_bytes(), key))
    read = registry.read('qwen3.8-27b-nvfp4@1')
    assert read.entry == verified.entry and read.entry.qualified == QUALIFIED
    assert 'qualified' not in read.entry.manifest  # beside the author's manifest, never inside it

    path = tmp_path / 'registry' / 'qwen3.8-27b-nvfp4@1.json'
    path.write_bytes(path.read_bytes().replace(b'"load_s":16.5', b'"load_s":9.5'))  # signed with the rest
    with pytest.raises(RegistryError, match='does not verify'):
        registry.read('qwen3.8-27b-nvfp4@1')

    # every entry blessed before the block existed: the same bytes as ever, and it still verifies
    old_registry, old = blessed(tmp_path / 'old', release)
    fields = {k: old.entry.as_dict()[k] for k in ('name', 'version', 'image', 'manifest', 'blessed_at')}
    assert old.entry.canonical_bytes() == canonical_json(fields)
    assert old_registry.read('qwen3.8-27b-nvfp4@1').entry.qualified is None


def test_a_malformed_qualified_block_is_refused():
    document = yaml.safe_load(FIXTURE.read_text())
    without_driver = {k: v for k, v in QUALIFIED.items() if k != 'driver'}
    cases: tuple[tuple[Any, str], ...] = (
        ({**QUALIFIED, 'load_s': '16.5'}, 'load_s must be a number'),
        (without_driver, 'driver missing'),
        ({**QUALIFIED, 'health_ok': 1}, 'health_ok must be a boolean'),
        ({**QUALIFIED, 'vram_gb': float('nan')}, 'vram_gb must be a number'),
        ({**QUALIFIED, 'tokens': 3}, r'unknown field\(s\): tokens'),
        ({**QUALIFIED, 'box': ' '}, 'hotkey or "local"'),
        ([], 'expected an object'),
    )
    for block, why in cases:
        with pytest.raises(RegistryError, match=why):
            make_entry(document, IMAGE, qualified=block)  # pyright: ignore[reportArgumentType]  (wrong types on purpose)


def test_bless_qualified_and_registry_show_print_it(tmp_path, release, monkeypatch):
    from tests.controller.test_cli import invoke  # loads the CLI package first (circular import)

    monkeypatch.setenv('COLUMNS', '300')
    key, _ = release
    manifest = tmp_path / 'manifest.yaml'
    manifest.write_text(FIXTURE.read_text())
    qualified = tmp_path / 'qualified.json'
    qualified.write_text(json.dumps(QUALIFIED))
    common = ['--release-pubkey', tmp_path / 'release.pub', '--state-dir', tmp_path / 'state']
    blessed_ = invoke('bless', manifest, '--image', IMAGE, '--sign-key', key, '--qualified', qualified, *common)
    assert blessed_.exit_code == 0, blessed_.output
    (row,) = json.loads(invoke('registry', 'show', *common, '--json').stdout)['entries']
    assert row['verified'] and row['qualified'] == QUALIFIED
    shown = invoke('registry', 'show', *common).output
    assert 'local, driver 580.65.06' in shown and 'load 16.5 s' in shown and '99.0 tok/s single' in shown

    bad = tmp_path / 'bad.json'
    bad.write_text('{"box": "local"}')
    refused = invoke('bless', manifest, '--image', IMAGE, '--sign-key', key, '--qualified', bad, *common)
    assert refused.exit_code == 1 and 'qualified: at missing' in refused.output


def test_source_image_is_signed_as_provenance_and_must_carry_the_same_digest(tmp_path, release, monkeypatch):
    from tests.controller.test_cli import invoke  # loads the CLI package first (circular import)

    key, pub = release
    source = 'ghcr.io/gittensor-ai-lab/sparkinfer-qwen38:v0.5.8@sha256:' + 'ab' * 32
    document = yaml.safe_load(FIXTURE.read_text())
    registry = Registry(tmp_path / 'registry', pub)
    verified = make_entry(document, IMAGE, now=1_757_000_000, source_image=source)
    registry.write(verified, sign_bytes(verified.entry.canonical_bytes(), key))
    read = registry.read('qwen3.8-27b-nvfp4@1').entry
    assert read.image == IMAGE and read.source_image == source  # boxes pull ours; the author's is the record
    assert 'source_image' not in read.manifest
    for bad in ('ghcr.io/gittensor-ai-lab/sparkinfer-qwen38:v0.5.8', source[:-64] + 'cd' * 32, 7):
        with pytest.raises(RegistryError, match='source_image'):
            make_entry(document, IMAGE, source_image=bad)  # pyright: ignore[reportArgumentType]

    manifest = tmp_path / 'manifest.yaml'
    manifest.write_text(FIXTURE.read_text())
    common = ['--release-pubkey', tmp_path / 'release.pub', '--state-dir', tmp_path / 'state']
    bless = ['bless', manifest, '--image', IMAGE, '--sign-key', key]
    refused = invoke(*bless, '--source-image', source[:-64] + 'cd' * 32, *common)
    assert refused.exit_code == 1 and 'source_image' in refused.output
    assert invoke(*bless, '--source-image', source, *common).exit_code == 0
    (row,) = json.loads(invoke('registry', 'show', *common, '--json').stdout)['entries']
    assert row['verified'] and row['image'] == IMAGE and row['source_image'] == source
    # the same entry without its provenance is different content
    assert 'already blessed with different content' in invoke(*bless, *common).output


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
    assert (
        vars(reopened.get('x@1')) == {'enabled': False, 'replicas': 2, 'box': ''} and reopened.get('x@1').desired == 0
    )
    with pytest.raises(ValueError):
        store.set('x@1', replicas=-1)


def test_a_deployment_can_be_pinned_to_a_box_and_unpinned(tmp_path):
    store = DeploymentStore(tmp_path / 'deployments.json')
    assert store.set('e@1', True, 1, box='hk-ours').box == 'hk-ours'
    assert DeploymentStore(tmp_path / 'deployments.json').get('e@1').box == 'hk-ours'
    assert store.set('e@1', replicas=2).box == 'hk-ours'  # untouched when not passed
    assert store.set('e@1', box='').box == ''


def test_every_committed_registry_entry_verifies_against_the_compiled_release_key():
    """The public record in docker/controller/registry/ must stay byte-exact: a formatter that appends a newline
    breaks the signature and the controller refuses the entry (9/17: end-of-file-fixer on qwen3.8-27b-nvfp4@6)."""
    from gittensor.controller.registry import load_release_pubkey

    committed = Path(__file__).resolve().parents[2] / 'docker' / 'controller' / 'registry'
    registry = Registry(committed, load_release_pubkey(None))
    entries = sorted(p.name[: -len('.json')] for p in committed.glob('*@*.json'))
    assert entries, 'no blessed entries committed'
    for entry_id in entries:
        assert registry.read(entry_id).entry_id == entry_id
