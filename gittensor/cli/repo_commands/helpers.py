# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Repo-registry CLI helpers: contract resolution, client factories, id/name
resolution, and the hyperparam name↔key table. Generic infra lives in
gittensor.cli.core."""

import os
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

import click
import requests

from gittensor.cli.core.helpers import (
    GITHUB_API_TIMEOUT,
    load_config,
    resolve_network,
    resolve_wallet_config,
)
from gittensor.cli.core.json_output import emit_error_json, emit_json
from gittensor.constants import BASE_GITHUB_API_URL

FP6 = 1_000_000
WEIGHT_SUM = 65_535
DEFAULT_BASKET_CAP = 10

# Hyperparam name -> (contract key, fixed-point FP6). Source: spec param table
# and smart-contracts/repos-v0/types.rs DEFAULT_BOUNDS.
PARAM_KEYS: Dict[str, Tuple[int, bool]] = {
    'issue_discovery_share': (1, True),
    'default_label_multiplier': (2, True),
    'fixed_base_score': (3, True),
    'maintainer_cut': (4, True),
    'trusted_label_pipeline': (5, False),
    'min_valid_merged_prs': (6, False),
    'min_credibility': (7, True),
    'excessive_pr_penalty_base_threshold': (8, False),
    'open_pr_threshold_token_score': (9, True),
    'max_open_pr_threshold': (10, False),
    'min_valid_solved_issues': (11, False),
    'min_issue_credibility': (12, True),
    'min_token_score_for_valid_issue': (13, True),
    'open_issue_spam_base_threshold': (14, False),
    'open_issue_spam_token_score_per_slot': (15, True),
    'max_open_issue_threshold': (16, False),
    'pr_lookback_days': (17, False),
    'open_pr_collateral_percent': (18, True),
    'review_penalty_rate': (19, True),
    'standard_issue_multiplier': (20, True),
    'maintainer_issue_multiplier': (21, True),
    'src_tok_saturation_scale': (22, True),
    'grace_period_hours': (23, False),
    'sigmoid_midpoint_days': (24, True),
    'sigmoid_steepness': (25, True),
    'time_decay_min_multiplier': (26, True),
}

PARAM_NAMES_BY_KEY: Dict[int, str] = {key: name for name, (key, _) in PARAM_KEYS.items()}


def get_repos_contract_address(cli_value: str = '') -> str:
    """Resolve the repos contract address: --contract > config > env."""
    if cli_value:
        return cli_value
    config_value = load_config().get('repos_contract_address')
    if config_value:
        return config_value
    return os.environ.get('REPOS_CONTRACT_ADDRESS', '')


def resolve_repos_contract_and_network(
    contract: str,
    network: Optional[str] = None,
    rpc_url: Optional[str] = None,
) -> Tuple[str, str, str]:
    """Resolve (contract_addr, ws_endpoint, network_name) for the repos contract."""
    contract_addr = get_repos_contract_address(contract)
    ws_endpoint, network_name = resolve_network(network, rpc_url)
    if not contract_addr:
        raise click.ClickException(
            'Repos contract address not configured. '
            'Set it with `gitt config set repos_contract_address <ADDR>` or REPOS_CONTRACT_ADDRESS.'
        )
    return contract_addr, ws_endpoint, network_name


def make_registry_client(contract_addr: str, ws_endpoint: str):
    """Connect and build a RepoRegistryContractClient. Returns (subtensor, client)."""
    import bittensor as bt

    from gittensor.validator.repo_registry.contract_client import RepoRegistryContractClient

    subtensor = bt.Subtensor(network=ws_endpoint)
    client = RepoRegistryContractClient(contract_address=contract_addr, subtensor=subtensor)
    return subtensor, client


def make_registry_wallet_client(contract_addr: str, ws_endpoint: str, wallet_name: str, wallet_hotkey: str):
    """Resolve wallet config and build client. Returns (wallet, subtensor, client)."""
    import bittensor as bt

    effective_wallet, effective_hotkey = resolve_wallet_config(wallet_name, wallet_hotkey)
    wallet = bt.Wallet(name=effective_wallet, hotkey=effective_hotkey)
    subtensor, client = make_registry_client(contract_addr, ws_endpoint)
    return wallet, subtensor, client


def resolve_repo_ref(ref: str, client, at: Optional[str] = None):
    """Resolve a github id or owner/name to a ContractRepo. Raises ClickException."""
    if ref.isdigit():
        repo = client.get_repo(int(ref), at=at)
        if repo is None:
            raise click.ClickException(f'Repo id {ref} not found in the registry.')
        return repo
    name = ref.strip().lower()
    for repo in client.get_all_repos(at=at):
        if repo.full_name == name:
            return repo
    raise click.ClickException(f"Repository '{ref}' not found in the registry.")


def resolve_github_repo_id(full_name: str) -> int:
    """Resolve owner/name to the GitHub numeric repo id (strict: any failure aborts)."""
    try:
        resp = requests.get(
            f'{BASE_GITHUB_API_URL}/repos/{full_name}',
            headers={'User-Agent': 'gittensor-cli'},
            timeout=GITHUB_API_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise click.ClickException(
            f'Could not resolve {full_name} on GitHub ({type(exc).__name__}). Use --id to register offline.'
        )
    if resp.status_code == 404:
        raise click.ClickException(f"Repository '{full_name}' not found on GitHub.")
    if not resp.ok:
        raise click.ClickException(
            f'GitHub API returned {resp.status_code} resolving {full_name}. Use --id to register offline.'
        )
    github_id = resp.json().get('id')
    if not isinstance(github_id, int) or github_id <= 0:
        raise click.ClickException(f'GitHub returned no numeric id for {full_name}.')
    return github_id


def parse_param_value(name: str, raw: str) -> int:
    """Parse a human param value into contract units (FP6 keys scale by 1e6)."""
    _, fp6 = PARAM_KEYS[name]
    try:
        d = Decimal(raw.strip())
    except InvalidOperation:
        raise click.BadParameter(f'Invalid number for {name}: {raw!r}', param_hint=name)
    if not d.is_finite() or d < 0:
        raise click.BadParameter(f'{name} must be a non-negative finite number', param_hint=name)
    scaled = d * FP6 if fp6 else d
    if scaled != scaled.to_integral_value():
        unit = 'at most 6 decimal places' if fp6 else 'an integer'
        raise click.BadParameter(f'{name} must be {unit} (got {raw})', param_hint=name)
    return int(scaled)


def format_param_value(key: int, raw: int) -> str:
    """Format a contract param value for humans (FP6 keys divide by 1e6)."""
    name = PARAM_NAMES_BY_KEY.get(key)
    if name is None or not PARAM_KEYS[name][1]:
        return str(raw)
    return f'{Decimal(raw) / FP6:f}'


def param_table_hint() -> str:
    """One-line-per-param hint listing valid names for error output."""
    return '\n'.join(f'  {key:>2}  {name}' for name, (key, _) in PARAM_KEYS.items())


def emit_tx_result(
    action: str,
    tx_hash: Optional[str],
    error: Optional[str],
    as_json: bool,
    **extra: Any,
) -> None:
    """Emit a transaction outcome in JSON or human form; exits non-zero on failure."""
    from gittensor.cli.core.helpers import console, print_error, print_success

    if error is None and tx_hash is not None:
        if as_json:
            emit_json({'success': True, 'action': action, 'tx_hash': tx_hash, **extra})
        else:
            print_success(f'{action} succeeded!')
            console.print(f'[cyan]Transaction Hash:[/cyan] {tx_hash}')
        return
    message = error or f'{action} failed'
    if as_json:
        emit_error_json(message, error_type='tx_failed', tx_hash=tx_hash)
    else:
        print_error(message)
        if tx_hash is not None:
            console.print(f'[cyan]Extrinsic Hash:[/cyan] {tx_hash}')
    raise SystemExit(1)
