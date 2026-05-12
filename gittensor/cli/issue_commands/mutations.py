# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Top-level mutation commands for issue CLI

Commands:
    gitt register
    gitt harvest
"""

from pathlib import Path

import click
from rich.panel import Panel

from .help import StyledCommand
from .helpers import (
    BountyAlphaParam,
    GITHUB_ISSUE_NUMBER_TYPE,
    NETWORK_CHOICE,
    _is_interactive,
    _resolve_contract_and_network,
    console,
    err_console,
    format_alpha,
    load_config,
    print_error,
    print_network_header,
    print_success,
    validate_github_issue,
    validate_repository,
)


def _print_register_revert_hints() -> None:
    print_error('Contract rejected the request')
    err_console.print('[yellow]Possible reasons:[/yellow]')
    err_console.print('  • Issue already registered (same repo + issue number)')
    err_console.print('  • Bounty too low (minimum 10 ALPHA)')
    err_console.print('  • Caller is not the contract owner')


@click.command('register', cls=StyledCommand)
@click.option(
    '--repo',
    required=True,
    help='Repository in owner/repo format (e.g., latent-to/btcli)',
)
@click.option(
    '--issue',
    'issue_number',
    required=True,
    type=GITHUB_ISSUE_NUMBER_TYPE,
    help='GitHub issue number (1–4294967295)',
)
@click.option(
    '--bounty',
    required=True,
    type=BountyAlphaParam(),
    help='Bounty amount in ALPHA (e.g. 10 or 10.5)',
)
@click.option(
    '--network',
    '-n',
    default=None,
    type=NETWORK_CHOICE,
    help='Network (finney/test/local)',
)
@click.option(
    '--rpc-url',
    default=None,
    help='Subtensor RPC endpoint (overrides --network)',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses default if empty)',
)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='default',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name',
)
@click.option(
    '--yes',
    '-y',
    is_flag=True,
    help='Skip confirmation prompt (non-interactive/CI)',
)
def issue_register(
    repo: str,
    issue_number: int,
    bounty: int,
    network: str,
    rpc_url: str,
    contract: str,
    wallet_name: str,
    wallet_hotkey: str,
    yes: bool,
):
    """
    Register a new issue with a bounty (OWNER ONLY).

    [dim]This command registers a GitHub issue on the smart contract with a target bounty amount.
    Only the contract owner can register new issues.[/dim]

    [dim]Arguments:
        --repo: Repository in owner/repo format
        --issue: GitHub issue number
        --bounty: Target bounty amount in ALPHA
    [/dim]

    [dim]Examples:
        $ gitt issues register --repo latent-to/btcli --issue 144 --bounty 100
        $ gitt i reg --repo tensorflow/tensorflow --issue 12345 --bounty 50
        $ gitt i reg --repo owner/repo --issue 1 --bounty 10 -y
    [/dim]
    """
    err_console.print('\n[bold cyan]Register Issue for Bounty[/bold cyan]\n')

    # Validate inputs before touching chain config. Bounty and GitHub issue
    # number are already checked at Click parse time; repo/issue still need
    # strict GitHub verification before any RPC work.
    try:
        owner, repo_name = validate_repository(repo, require_verified_exists=True)
        validate_github_issue(owner, repo_name, issue_number, require_verified_exists=True)
    except click.BadParameter as e:
        raise click.ClickException(str(e))

    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(
        contract,
        network,
        rpc_url,
        missing_contract_message='Contract address not configured. Run ./up.sh --issues to deploy the contract first.',
    )
    config = load_config()

    github_url = f'https://github.com/{repo}/issues/{issue_number}'

    err_console.print(
        Panel(
            f'[cyan]Repository:[/cyan] {repo}\n'
            f'[cyan]Issue Number:[/cyan] #{issue_number}\n'
            f'[cyan]GitHub URL:[/cyan] {github_url}\n'
            f'[cyan]Target Bounty:[/cyan] {format_alpha(bounty, 2)} ALPHA\n'
            f'[cyan]Network:[/cyan] {network_name}\n'
            f'[cyan]RPC Endpoint:[/cyan] {ws_endpoint}\n'
            f'[cyan]Contract:[/cyan] {contract_addr}',
            title='Issue Registration',
            border_style='blue',
        )
    )

    skip_confirm = yes or not _is_interactive()
    if not skip_confirm and not click.confirm('\nProceed with registration?', default=True):
        err_console.print('[yellow]Registration cancelled.[/yellow]')
        return

    try:
        import bittensor as bt
        from substrateinterface import Keypair, SubstrateInterface
        from substrateinterface.contracts import ContractInstance

        with err_console.status('[bold cyan]Connecting to network...', spinner='dots'):
            substrate = SubstrateInterface(url=ws_endpoint)

        # CLI flags override config; fall back to config if not explicitly supplied
        effective_wallet = wallet_name if wallet_name != 'default' else config.get('wallet', wallet_name)
        effective_hotkey = wallet_hotkey if wallet_hotkey != 'default' else config.get('hotkey', wallet_hotkey)

        # For local development, check config first, then fall back to //Alice
        if network_name.lower() == 'local' and effective_wallet == 'default' and effective_hotkey == 'default':
            err_console.print('[dim]Using //Alice for local development (no config set)...[/dim]')
            keypair = Keypair.create_from_uri('//Alice')
        else:
            # Load wallet from config or CLI args
            err_console.print(f'[dim]Loading wallet {effective_wallet}/{effective_hotkey}...[/dim]')
            wallet = bt.Wallet(name=effective_wallet, hotkey=effective_hotkey)
            # Use COLDKEY for owner-only operations (register_issue requires owner)
            # Contract owner is set to deployer's coldkey during contract instantiation
            keypair = wallet.coldkey

        # Load contract
        # Go up 4 levels: mutations.py -> issue_commands -> cli -> gittensor -> REPO_ROOT
        contract_metadata = (
            Path(__file__).parent.parent.parent.parent
            / 'smart-contracts'
            / 'issues-v0'
            / 'target'
            / 'ink'
            / 'issue_bounty_manager.contract'
        )
        if not contract_metadata.exists():
            print_error(f'Contract metadata not found at {contract_metadata}')
            raise SystemExit(1)

        contract_instance = ContractInstance.create_from_address(
            contract_address=contract_addr,
            metadata_file=str(contract_metadata),
            substrate=substrate,
        )

        err_console.print('[dim]Submitting transaction...[/dim]')

        result = contract_instance.exec(
            keypair,  # type: ignore[arg-type]
            'register_issue',
            args={
                'github_url': github_url,
                'repository_full_name': repo,
                'issue_number': issue_number,
                'target_bounty': bounty,
            },
            gas_limit={'ref_time': 10_000_000_000, 'proof_size': 1_000_000},
        )

        # Check if transaction was successful
        if hasattr(result, 'is_success') and not result.is_success:
            error_info = getattr(result, 'error_message', None)
            is_revert = error_info and isinstance(error_info, dict) and error_info.get('name') == 'ContractReverted'

            if is_revert:
                _print_register_revert_hints()
            elif error_info:
                print_error(str(error_info))

            console.print(f'[cyan]Transaction Hash:[/cyan] {result.extrinsic_hash}')
            raise SystemExit(1)

        print_success('Issue registered successfully!')
        console.print(f'[cyan]Transaction Hash:[/cyan] {result.extrinsic_hash}')
        err_console.print('[dim]Issue will be visible once bounty is funded via harvest_emissions()[/dim]')

    except ImportError as e:
        print_error(f'Missing dependency - {e}')
        err_console.print('[dim]Install with: uv sync[/dim]')
        raise SystemExit(1)
    except Exception as e:
        error_msg = str(e)
        if 'ContractReverted' in error_msg:
            _print_register_revert_hints()
        else:
            print_error(f'Error registering issue: {e}')
        raise SystemExit(1)


@click.command('harvest', cls=StyledCommand)
@click.option(
    '--wallet-name',
    '--wallet.name',
    '--wallet',
    default='validator',
    help='Wallet name',
)
@click.option(
    '--wallet-hotkey',
    '--wallet.hotkey',
    '--hotkey',
    default='default',
    help='Hotkey name',
)
@click.option(
    '--network',
    '-n',
    default=None,
    type=NETWORK_CHOICE,
    help='Network (finney/test/local)',
)
@click.option(
    '--rpc-url',
    default=None,
    help='Subtensor RPC endpoint (overrides --network)',
)
@click.option(
    '--contract',
    default='',
    help='Contract address (uses config if empty)',
)
@click.option('--verbose', '-v', is_flag=True, help='Show detailed output')
def issue_harvest(wallet_name: str, wallet_hotkey: str, network: str, rpc_url: str, contract: str, verbose: bool):
    """
    Manually trigger emission harvest from contract treasury.

    [dim]This command is permissionless - any wallet can trigger it.
    The contract handles emission collection and distribution internally.[/dim]

    [dim]Examples:
        $ gitt harvest
        $ gitt harvest --verbose
        $ gitt harvest --wallet-name mywallet --wallet-hotkey mykey
    [/dim]
    """
    err_console.print('\n[bold cyan]Manual Emission Harvest[/bold cyan]\n')

    contract_addr, ws_endpoint, network_name = _resolve_contract_and_network(
        contract,
        network,
        rpc_url,
        missing_contract_message='Contract address not configured. Set CONTRACT_ADDRESS env var or run ./up.sh --issues.',
    )

    print_network_header(network_name, contract_addr)
    err_console.print(f'[dim]Wallet: {wallet_name}/{wallet_hotkey}[/dim]\n')

    try:
        import bittensor as bt

        from gittensor.validator.issue_competitions.contract_client import (
            IssueCompetitionContractClient,
        )

        with err_console.status('[bold cyan]Loading wallet...', spinner='dots'):
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            hotkey_addr = wallet.hotkey.ss58_address
        err_console.print(f'[green]Hotkey address:[/green] {hotkey_addr}')

        with err_console.status('[bold cyan]Connecting to network...', spinner='dots'):
            subtensor = bt.Subtensor(network=ws_endpoint)

        # Show wallet balance (informational only)
        if verbose:
            try:
                balance = subtensor.get_balance(hotkey_addr)
                err_console.print(f'[dim]Wallet balance: {balance}[/dim]')
            except Exception as e:
                err_console.print(f'[dim]Could not fetch balance: {e}[/dim]')

        with err_console.status('[bold cyan]Initializing contract client...', spinner='dots'):
            client = IssueCompetitionContractClient(
                contract_address=contract_addr,
                subtensor=subtensor,
            )

        if verbose:
            # Show contract state
            err_console.print('[dim]Reading contract state...[/dim]')
            try:
                alpha_pool = client.get_alpha_pool()
                pending = client.get_treasury_stake()
                last_harvest = client.get_last_harvest_block()
                current_block = subtensor.get_current_block()

                err_console.print(f'[dim]Alpha pool: {format_alpha(alpha_pool, 4)} ALPHA[/dim]')
                err_console.print(f'[dim]Treasury stake: {format_alpha(pending, 4)} ALPHA[/dim]')
                err_console.print(f'[dim]Last harvest block: {last_harvest}[/dim]')
                err_console.print(f'[dim]Current block: {current_block}[/dim]')
                if last_harvest > 0:
                    err_console.print(f'[dim]Blocks since harvest: {current_block - last_harvest}[/dim]')
            except Exception as e:
                err_console.print(f'[yellow]Warning: Could not read contract state: {e}[/yellow]')

        with err_console.status('[bold cyan]Calling harvest_emissions()...', spinner='dots'):
            result = client.harvest_emissions(wallet)

        if result:
            if result.get('status') == 'success':
                print_success('Harvest succeeded!')
                console.print(f'[cyan]Transaction hash:[/cyan] {result.get("tx_hash", "N/A")}')
                err_console.print('[dim]Treasury stake processed. Excess emissions recycled if any.[/dim]')
            elif result.get('status') == 'partial':
                err_console.print('\n[yellow]Harvest completed but recycling failed[/yellow]')
                console.print(f'[cyan]Transaction hash:[/cyan] {result.get("tx_hash", "N/A")}')
                print_error(result.get('error', 'Unknown'))
                err_console.print('[dim]Check proxy permissions: contract needs NonCritical proxy.[/dim]')
                raise SystemExit(1)
            elif result.get('status') in {'failed', 'error'}:
                print_error(f'Harvest failed: {result.get("error", "Unknown error")}')
                raise SystemExit(1)
            else:
                err_console.print(f'\n[yellow]Harvest result: {result}[/yellow]')
                raise SystemExit(1)
        else:
            print_error('Harvest returned None — check logs for details.')
            err_console.print('[dim]Run with --verbose for more information.[/dim]')
            raise SystemExit(1)

    except ImportError as e:
        print_error(f'Missing dependency — {e}')
        err_console.print('[dim]Install with: uv sync[/dim]')
        raise SystemExit(1)
    except Exception as e:
        import traceback

        print_error(f'{type(e).__name__}: {e}')
        if verbose:
            err_console.print(f'[dim]Full traceback:\n{traceback.format_exc()}[/dim]')
        else:
            err_console.print('[dim]Run with --verbose for full traceback.[/dim]')
        raise SystemExit(1)
