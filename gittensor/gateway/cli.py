# The MIT License (MIT)
# Copyright © 2025 Entrius

"""``gitt gateway``: the compute pool's request path, in its own container beside the controller (vault ``26`` §1-2).

    GT_GATEWAY_KEY=<secret> gitt gateway --state-dir ~/.gittensor/controller [--listen 0.0.0.0:8790] [--refresh 3]

It reads the controller's state directory (``instances.json``, ``tunnels.json``, ``registry/``,
``models_override.json``) and writes nothing there; usage lines go to stdout, logs to stderr. Start
``gitt controller tunnels`` first: an instance is reached through its tunnel or not at all.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import click

DEFAULT_STATE_DIR = Path.home() / '.gittensor' / 'controller'
KEY_ENV = 'GT_GATEWAY_KEY'


def _listen(ctx, param, value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(':')
    if not sep or not port.isdigit() or not 0 < int(port) < 65536:
        raise click.BadParameter(f'{value!r}: expected host:port')
    return host.strip('[]') or '0.0.0.0', int(port)


@click.command('gateway')
@click.option(
    '--state-dir',
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_STATE_DIR,
    show_default=True,
    help="The controller's state directory, read only: instances.json, tunnels.json, registry/, models_override.json.",
)
@click.option('--listen', default='0.0.0.0:8790', show_default=True, callback=_listen, help='host:port to serve on.')
@click.option(
    '--refresh', type=click.FloatRange(min=0.1), default=3.0, show_default=True, help='Seconds between table reads.'
)
@click.option('--request-timeout', type=click.FloatRange(min=1.0), default=600.0, show_default=True, help='Seconds.')
@click.option('--max-body-bytes', type=click.IntRange(min=1024), default=16 * 1024 * 1024, show_default=True)
@click.option(
    '--release-pubkey',
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help='Public key registry entries must verify against (default: the compiled release key).',
)
@click.option('--allow-dev-keys', is_flag=True, default=False, help='Trust a --release-pubkey tagged DO-NOT-SHIP.')
@click.option(
    '--allow-direct',
    is_flag=True,
    default=False,
    help="Address an instance at its record's host and port when it has no tunnel; the previous behaviour.",
)
def gateway_command(
    state_dir, listen, refresh, request_timeout, max_body_bytes, release_pubkey, allow_dev_keys, allow_direct
):
    """Route OpenAI-style requests onto healthy leased instances: take a free slot or 429, never queue.

    \b
    Every request but GET /healthz needs the X-GT-Gateway-Key header equal to $GT_GATEWAY_KEY.
    An instance is reached through its tunnel (tunnels.json, from `gitt controller tunnels`).
    """
    key = os.environ.get(KEY_ENV, '')
    if not key:
        click.echo(f'Error: {KEY_ENV} is not set; refusing to start without the gateway key', err=True)
        sys.exit(2)
    # Imported here, not at module load: `gitt` registers every command on each start.
    import uvicorn

    from gittensor.controller.registry import Registry, RegistryError, load_release_pubkey
    from gittensor.gateway.app import Gateway, GatewayConfig, build_app
    from gittensor.gateway.table import InstanceTable

    state_dir = Path(state_dir).expanduser()
    try:
        registry = Registry(state_dir / 'registry', load_release_pubkey(release_pubkey, allow_dev_keys))
    except RegistryError as e:
        click.echo(f'Error: {e}', err=True)
        sys.exit(2)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    config = GatewayConfig(key=key, refresh_s=refresh, request_timeout_s=request_timeout, max_body_bytes=max_body_bytes)
    gateway = Gateway(config, InstanceTable(state_dir, registry, allow_direct=allow_direct))
    host, port = listen
    path = 'tunnels, else the record address' if allow_direct else 'tunnels only'
    click.echo(f'gateway on {host}:{port}, state {state_dir}, refresh {refresh:g} s, instances via {path}', err=True)
    uvicorn.run(build_app(gateway), host=host, port=port, log_level='warning', loop='asyncio')


def register_gateway_commands(cli):
    """Register `gitt gateway` with the root CLI group."""
    cli.add_command(gateway_command, name='gateway')
