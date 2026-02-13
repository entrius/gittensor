"""
Update metadata.json from compiled contract.

Run after: cargo contract build
Usage: python update_metadata.py
"""

import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent
CONTRACT_FILE = REPO_ROOT / 'smart-contracts' / 'issues-v0' / 'target' / 'ink' / 'issue_bounty_manager.contract'
METADATA_FILE = SCRIPT_DIR / 'metadata.json'

# Methods we use in the validator/CLI
METHODS_WE_USE = [
    'register_issue',
    'cancel_issue',
    'vote_solution',
    'vote_cancel_issue',
    'set_owner',
    'set_treasury_hotkey',
    'get_treasury_stake',
    'get_last_harvest_block',
    'harvest_emissions',
    'payout_bounty',
    'get_alpha_pool',
    'get_issue',
    'get_issues_by_status',
    'add_validator',
    'remove_validator',
    'get_validators',
]


def get_type_string(type_id: int, types: list) -> str:
    """Convert type ID to string representation."""
    for t in types:
        if t.get('id') == type_id:
            type_def = t.get('type', {}).get('def', {})
            path = t.get('type', {}).get('path', [])

            if 'primitive' in type_def:
                return type_def['primitive']
            if 'array' in type_def:
                if type_def['array'].get('len') == 32:
                    return 'array32'
                return 'array'
            if 'composite' in type_def:
                if path and 'AccountId' in path[-1]:
                    return 'AccountId'
    return 'unknown'


def main():
    if not CONTRACT_FILE.exists():
        print(f"Error: {CONTRACT_FILE} not found")
        print("Run 'cargo contract build' first")
        return 1

    with open(CONTRACT_FILE) as f:
        contract = json.load(f)

    types = contract.get('types', [])
    messages = contract.get('spec', {}).get('messages', [])

    # Extract selectors
    selectors = {}
    for msg in messages:
        name = msg['label']
        if name in METHODS_WE_USE:
            selectors[name] = msg['selector'].replace('0x', '')

    # Extract arg types
    arg_types = {}
    for msg in messages:
        name = msg['label']
        if name in METHODS_WE_USE:
            args = []
            for arg in msg.get('args', []):
                arg_name = arg['label']
                arg_type = get_type_string(arg['type']['type'], types)
                args.append([arg_name, arg_type])
            arg_types[name] = args

    # Write metadata.json
    metadata = {
        'selectors': selectors,
        'arg_types': arg_types,
    }

    with open(METADATA_FILE, 'w') as f:
        json.dump(metadata, f, indent=2)
        f.write('\n')

    print(f"Updated {METADATA_FILE}")
    print(f"  {len(selectors)} selectors")
    print(f"  {len(arg_types)} arg type mappings")


if __name__ == '__main__':
    exit(main() or 0)
