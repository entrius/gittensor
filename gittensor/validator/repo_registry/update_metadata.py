"""
Generate metadata.json for the repos-v0 registry client.

Selectors use ink!'s default derivation — the first 4 bytes of the blake2b-256
hash of the message label — so no `cargo contract build` is required. When the
built artifact (target/ink/repo_registry.contract) exists, its selectors are
cross-checked and any mismatch fails the run. Arg types are declared here;
source of truth is smart-contracts/repos-v0/lib.rs message signatures.

Mapping root keys are NOT part of this file: ink! 5 AutoKey roots are
XXH32('RepoRegistry::<field>') per ink_primitives KeyComposer, computed in
storage_utils.py (derivation verified against issues-v0's '52789899' key).

Usage: python update_metadata.py
"""

import hashlib
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent
CONTRACT_FILE = REPO_ROOT / 'smart-contracts' / 'repos-v0' / 'target' / 'ink' / 'repo_registry.contract'
METADATA_FILE = SCRIPT_DIR / 'metadata.json'

# Methods we use in the validator/CLI, with declared arg types (lib.rs signatures)
METHOD_ARG_TYPES = {
    'register': [['github_id', 'u64'], ['full_name', 'str'], ['fee_hotkey', 'AccountId']],
    'set_param': [['github_id', 'u64'], ['key', 'u8'], ['value', 'u64']],
    'set_label_multiplier': [['github_id', 'u64'], ['label', 'str'], ['value', 'u64']],
    'remove_label_multiplier': [['github_id', 'u64'], ['label', 'str']],
    'set_branch_patterns': [['github_id', 'u64'], ['patterns', 'vec_str']],
    'update_full_name': [['github_id', 'u64'], ['full_name', 'str']],
    'transfer_ownership': [['github_id', 'u64'], ['new_owner', 'AccountId']],
    'deregister': [['github_id', 'u64']],
    'set_basket': [['entries', 'vec_u64_u16']],
    'clear_basket': [],
}


def ink_selector(label: str) -> str:
    """ink! default message selector: blake2b-256(label)[..4]."""
    return hashlib.blake2b(label.encode(), digest_size=32).digest()[:4].hex()


def verify_against_contract_file(selectors: dict) -> None:
    """Cross-check derived selectors against a built contract artifact."""
    with open(CONTRACT_FILE) as f:
        contract = json.load(f)
    built = {msg['label']: msg['selector'].replace('0x', '') for msg in contract.get('spec', {}).get('messages', [])}
    for name, selector in selectors.items():
        if built.get(name) != selector:
            raise SystemExit(f'Selector mismatch for {name}: derived {selector}, built {built.get(name)}')
    print(f'Verified {len(selectors)} selectors against {CONTRACT_FILE}')


def main():
    selectors = {name: ink_selector(name) for name in METHOD_ARG_TYPES}

    if CONTRACT_FILE.exists():
        verify_against_contract_file(selectors)
    else:
        print(f'{CONTRACT_FILE} not built; using derived selectors (blake2b-256(label)[..4])')

    metadata = {
        'selectors': selectors,
        'arg_types': METHOD_ARG_TYPES,
    }

    with open(METADATA_FILE, 'w') as f:
        json.dump(metadata, f, indent=2)
        f.write('\n')

    print(f'Updated {METADATA_FILE}')
    print(f'  {len(selectors)} selectors')


if __name__ == '__main__':
    exit(main() or 0)
