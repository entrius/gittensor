# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Predict command (`gitt issues predict`)."""

import json as json_mod

import click
from gittensor.cli.issue_commands.tables import build_pr_table
from rich.panel import Panel

from .helpers import (
    _is_interactive,
    console,
    emit_json,
    fetch_issue_from_contract,
    fetch_issue_prs,
    get_contract_address,
    get_github_pat,
    handle_exception,
    loading_context,
    print_error,
    print_network_header,
    resolve_network,
    load_config,
    validate_issue_id,
    verify_miner_registration,
)


@click.command('predict')
@click.option(
    '--id',
    'issue_id',
    required=True,
    type=int,
    help='On-chain issue ID',
)
@click.option('--pr', 'pr_number', default=None, type=int, help='PR number to predict (use with --probability)')
@click.option('--probability', default=None, type=float, help='Probability for --pr in [0.0, 1.0]')
@click.option('--json-input', default=None, type=str, help='Batch predictions JSON: {"101": 0.85, "103": 0.10}')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation prompt')
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
    '--network',
    '-n',
    default=None,
    type=click.Choice(['finney', 'test', 'local'], case_sensitive=False),
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
@click.option('--verbose', '-v', is_flag=True, help='Show debug output')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON for scripting')
def issues_predict(
    issue_id: int,
    pr_number: int | None,
    probability: float | None,
    json_input: str | None,
    yes: bool,
    wallet_name: str,
    wallet_hotkey: str,
    network: str | None,
    rpc_url: str | None,
    contract: str,
    verbose: bool,
    as_json: bool
):
    """Submit miner predictions for PRs on a bountied issue.

    Validates an active on-chain issue, miner registration, matching PRs, and
    probability constraints (each in ``[0.0, 1.0]``, total ``<= 1.0``).

    \b
    Input modes:
      1. ``--pr <pr_number> --probability <0.0-1.0>`` for a single prediction
      2. ``--json-input '{"101": 0.85, "103": 0.10}'`` for batch predictions
      3. Interactive prompt (default when neither ``--pr`` nor ``--json-input`` is provided)

    \b
    Notes:
      - ``--yes/-y`` skips the confirmation prompt.
      - ``--pr/--probability`` and ``--json-input`` are mutually exclusive.
      - Current implementation validates and builds payload only (stub send path).

    \b
    Examples:
        gitt i predict --id 42 --pr 101 --probability 0.85 -y
        gitt i predict --id 42 --json-input '{"101": 0.5, "103": 0.3}' -y
        gitt i predict --id 42
        gitt i predict --id 42 --pr 101 --probability 0.7 -y --json
    """
    # 1) Validate on-chain issue ID.
    try:
        validate_issue_id(issue_id, 'id')
    except click.BadParameter as e:
        handle_exception(as_json, str(e), 'bad_parameter')

    # 2) Validate prediction mode and parse JSON batch input (if provided).
    try:
        parsed_json_predictions = _prevalidate_prediction_inputs(pr_number, probability, json_input)
    except (click.BadParameter, click.ClickException) as e:
        handle_exception(as_json, str(e))

    # 3) Determine execution mode from validated inputs.
    is_batch_mode = parsed_json_predictions is not None
    is_single_pr_mode = pr_number is not None
    is_interactive_mode = not is_batch_mode and not is_single_pr_mode

    if is_interactive_mode and as_json:
        handle_exception(as_json, '--json mode requires --pr/--probability or --json-input.')

    # 4) Resolve network/contract context.
    contract_addr = get_contract_address(contract)
    _require_contract_address(contract_addr, as_json)
    ws_endpoint, network_name = resolve_network(network, rpc_url)
    effective_wallet, effective_hotkey = _resolve_wallet_identity(wallet_name, wallet_hotkey)

    if not as_json:
        print_network_header(network_name, contract_addr)
        console.print(f'[dim]Wallet: {effective_wallet}/{effective_hotkey}[/dim]\n')

    # 5) Resolve issue + fetch eligible open PR submissions.
    repo_full_name, issue_number = _resolve_issue_context(
        ws_endpoint=ws_endpoint,
        contract_addr=contract_addr,
        issue_id=issue_id,
        verbose=verbose,
        as_json=as_json,
    )

    pull_requests = _fetch_open_issue_pull_requests(
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        as_json=as_json,
    )

    if not pull_requests:
        handle_exception(as_json, 'No open PR submissions found for this issue.')

    # 6) Show submissions table only for interactive mode.
    if is_interactive_mode:
        console.print(
            f'[bold cyan]Open PR submissions for issue {issue_id}[/bold cyan] '
            f'[dim]({repo_full_name}#{issue_number})[/dim]\n'
        )
        console.print(build_pr_table(pull_requests))
        skip_continue_prompt = yes or not _is_interactive()
        if not skip_continue_prompt and not click.confirm(
            'Ready to start prediction?', default=True
        ):
            console.print('[yellow]Prediction cancelled.[/yellow]')
            return

    # 7) Interactive mode: verify miner first to avoid wasting manual input.
    if is_interactive_mode:
        _resolve_registered_miner_hotkey(
            wallet_name=effective_wallet,
            wallet_hotkey=effective_hotkey,
            ws_endpoint=ws_endpoint,
            contract_addr=contract_addr,
            as_json=as_json,
        )

    # 8) Collect predictions by mode; validate PR membership for non-interactive modes.
    try:
        if is_interactive_mode:
            predictions = _collect_predictions_interactive(pull_requests)
        else:
            predictions = {pr_number: float(probability)} if is_single_pr_mode else parsed_json_predictions
            _validate_predictions_against_open_prs(predictions, pull_requests)
    except (click.ClickException, click.BadParameter) as e:
        handle_exception(as_json, str(e))

    # 9) Single/batch modes: verify miner after prediction payload validation.
    if not is_interactive_mode:
        _resolve_registered_miner_hotkey(
            wallet_name=effective_wallet,
            wallet_hotkey=effective_hotkey,
            ws_endpoint=ws_endpoint,
            contract_addr=contract_addr,
            as_json=as_json,
        )

    payload = build_prediction_payload(
        issue_id=issue_id,
        repository=repo_full_name,
        predictions=predictions,
    )

    # 10) Emit machine output or interactive confirmation flow.
    if as_json:
        emit_json(payload, pretty=True)
        broadcast_predictions_stub(payload)
        return

    lines = format_prediction_lines(predictions)
    lines += '\n\nThis is a stubbed send path. No network broadcast is executed yet.'
    console.print(Panel(lines, title='Prediction Confirmation', border_style='blue'))

    skip_confirm = yes or not _is_interactive()
    if not skip_confirm and not click.confirm('Proceed?', default=True):
        console.print('[yellow]Prediction cancelled.[/yellow]')
        return
    
    console.print(Panel(json_mod.dumps(payload, indent=2), title='Validated Payload (Stub)', border_style='green'))
    console.print('[green]Prediction payload prepared (broadcast TODO).[/green]')
    broadcast_predictions_stub(payload)


def validate_probability(value: float, param_hint: str = 'probability') -> float:
    """Validate probability is in the inclusive [0.0, 1.0] range."""
    if not (0.0 <= value <= 1.0):
        raise click.BadParameter(
            f'Probability must be between 0.0 and 1.0 (got {value})',
            param_hint=param_hint,
        )
    return value


def _validate_prediction_mode(
    pr_number: int | None,
    probability: float | None,
    json_input: str | None,
) -> tuple[bool, bool, bool]:
    """Validate mutually exclusive prediction input modes."""
    has_pr = pr_number is not None
    has_probability = probability is not None
    has_json_input = json_input is not None

    if has_json_input and (has_pr or has_probability):
        raise click.ClickException('Use either --pr/--probability or --json-input, not both.')
    if not has_pr and has_probability:
        raise click.ClickException('--probability requires --pr.')
    if has_pr and not has_probability:
        raise click.ClickException('--probability is required when --pr is set.')

    return has_pr, has_probability, has_json_input


def _require_contract_address(contract_addr: str, as_json: bool) -> None:
    """Require a configured contract address before network work."""
    if not contract_addr:
        if as_json:
            handle_exception(as_json, 'Contract address not configured')
        print_error('Contract address not configured')
        raise SystemExit(1)


def _resolve_wallet_identity(wallet_name: str, wallet_hotkey: str) -> tuple[str, str]:
    """Resolve effective wallet/hotkey names from CLI args and config defaults."""
    config = load_config()
    effective_wallet = wallet_name if wallet_name != 'default' else config.get('wallet', wallet_name)
    effective_hotkey = wallet_hotkey if wallet_hotkey != 'default' else config.get('hotkey', wallet_hotkey)
    return effective_wallet, effective_hotkey


def _resolve_issue_context(
    ws_endpoint: str,
    contract_addr: str,
    issue_id: int,
    verbose: bool,
    as_json: bool,
) -> tuple[str, int]:
    """Load and validate on-chain issue context for prediction."""
    try:
        with loading_context('[bold cyan]Reading issues from contract...', as_json):
            issue = fetch_issue_from_contract(
                ws_endpoint, contract_addr, issue_id, require_active=True, verbose=verbose
            )
    except click.ClickException as e:
        handle_exception(as_json, str(e))

    repo_full_name = str(issue.get('repository_full_name', ''))
    issue_number = int(issue.get('issue_number', 0))
    return repo_full_name, issue_number


def _fetch_open_issue_pull_requests(repo_full_name: str, issue_number: int, as_json: bool) -> list[dict]:
    """Fetch open PR submissions for the resolved repository issue."""
    token = get_github_pat() or ''
    if not token and not as_json:
        console.print('[dim]No GITTENSOR_MINER_PAT set; using unauthenticated GitHub API requests.[/dim]')

    try:
        with loading_context('[bold cyan]Fetching open PR submissions from GitHub...', as_json):
            return fetch_issue_prs(repo_full_name, issue_number, token, open_only=True)
    except click.ClickException as e:
        handle_exception(as_json, str(e))


def _resolve_registered_miner_hotkey(
    wallet_name: str,
    wallet_hotkey: str,
    ws_endpoint: str,
    contract_addr: str,
    as_json: bool,
) -> str:
    """Load wallet hotkey and ensure it is registered on the contract subnet."""
    try:
        import bittensor as bt

        with loading_context('[bold cyan]Validating miner identity and registration...', as_json):
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            miner_hotkey = wallet.hotkey.ss58_address
            is_registered = verify_miner_registration(ws_endpoint, contract_addr, miner_hotkey)
    except Exception as e:
        handle_exception(as_json, f'Failed to validate wallet/miner registration: {e}')

    if not is_registered:
        handle_exception(as_json, f'Wallet hotkey is not registered miner on subnet: {miner_hotkey}')
    return miner_hotkey


def _parse_json_predictions(json_input: str) -> dict[int, float]:
    """Parse and validate JSON batch predictions payload."""
    try:
        raw = json_mod.loads(json_input)
    except json_mod.JSONDecodeError as e:
        raise click.BadParameter(f'Invalid JSON: {e}', param_hint='--json-input')

    if not isinstance(raw, dict):
        raise click.BadParameter(
            'JSON input must be an object: {"pr_number": probability, ...}',
            param_hint='--json-input',
        )

    parsed_predictions: dict[int, float] = {}
    for key, value in raw.items():
        try:
            pr_num = int(key)
        except (TypeError, ValueError):
            raise click.BadParameter(f'Invalid PR number in JSON: {key}', param_hint='--json-input')
        try:
            parsed_predictions[pr_num] = validate_probability(float(value), '--json-input')
        except (TypeError, ValueError):
            raise click.BadParameter(
                f'Invalid probability value for PR #{key} in JSON: {value}',
                param_hint='--json-input',
            )

    if len(parsed_predictions) == 0:
        raise click.BadParameter(
            'JSON input must include at least one PR prediction.',
            param_hint='--json-input',
        )
    return parsed_predictions


def format_prediction_lines(predictions: dict[int, float]) -> str:
    """Format sorted prediction lines with running total."""
    lines = [f'PR #{pr_num}: {prob:.4f}' for pr_num, prob in sorted(predictions.items())]
    lines.append(f'Total: {sum(predictions.values()):.4f}')
    return '\n'.join(lines)


def build_prediction_payload(
    issue_id: int,
    repository: str,
    predictions: dict[int, float],
) -> dict[str, object]:
    """Build validated payload for future network broadcast."""
    return {
        'issue_id': issue_id,
        'repository': repository,
        'predictions': dict(predictions),
    }


def broadcast_predictions_stub(payload: dict[str, object]) -> None:
    """Broadcast integration seam (stub)."""
    pass

def _collect_predictions_interactive(prs: list[dict]) -> dict[int, float]:
    """Prompt for per-PR probabilities in interactive mode."""
    predictions: dict[int, float] = {}
    running_total = 0.0

    for pr in prs:
        number = pr.get('number')
        if not isinstance(number, int):
            continue

        while True:
            raw = click.prompt(
                f'Probability for PR #{number} (0.0-1.0, blank to skip)',
                default='',
                show_default=False,
            ).strip()
            if raw == '':
                break

            try:
                value = validate_probability(float(raw), f'PR #{number}')
            except ValueError:
                print_error(f'Invalid number: {raw}')
                continue
            except click.BadParameter as e:
                print_error(str(e))
                continue

            proposed_total = running_total + value
            if proposed_total > 1.0:
                print_error(f'Total probability cannot exceed 1.0 (current {running_total:.4f}, proposed {proposed_total:.4f})')
                continue

            predictions[number] = value
            running_total = proposed_total
            if running_total >= 0.99:
                console.print(f'[yellow]Running total: {running_total:.4f} (approaching 1.0)[/yellow]')
            else:
                console.print(f'[dim]Running total: {running_total:.4f}[/dim]')
            break

    if not predictions:
        raise click.ClickException('No predictions entered.')

    return predictions


def _validate_predictions_against_open_prs(
    predictions: dict[int, float],
    prs: list[dict],
    param_hint: str = 'predictions',
) -> None:
    """Validate PR IDs exist in open PRs for this issue and total is <= 1.0."""
    valid_pr_numbers = {int(p.get('number')) for p in prs if isinstance(p.get('number'), int)}
    for number in predictions:
        if number not in valid_pr_numbers:
            available = sorted(valid_pr_numbers)
            raise click.BadParameter(
                f'PR #{number} is not an open PR for this issue. Open PRs: {available}',
                param_hint=param_hint,
            )
    _validate_prediction_total(predictions, param_hint)


def _validate_prediction_total(predictions: dict[int, float], param_hint: str) -> None:
    """Validate that prediction probability total does not exceed 1.0."""
    total = sum(predictions.values())
    if total > 1.0:
        raise click.BadParameter(
            f'Sum of probabilities must be <= 1.0 (got {total:.4f})',
            param_hint=param_hint,
        )


def _prevalidate_prediction_inputs(
    pr_number: int | None,
    probability: float | None,
    json_input: str | None,
) -> dict[int, float] | None:
    """Validate CLI prediction inputs before any network I/O."""
    _, has_probability, has_json_input = _validate_prediction_mode(pr_number, probability, json_input)

    if has_probability:
        validate_probability(float(probability), '--probability')

    if not has_json_input:
        return None

    parsed_predictions = _parse_json_predictions(str(json_input))
    _validate_prediction_total(parsed_predictions, '--json-input')
    return parsed_predictions
