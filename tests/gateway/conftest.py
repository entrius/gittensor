# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Fixtures for the gateway: a fake OpenAI runtime (an asyncio aiohttp server that records exactly what reached it and
streams), a controller state directory with a signed registry, an ``instances.json`` and a ``tunnels.json``, and the
gateway itself on a real socket."""

import asyncio
import json
import socket
import subprocess
import time
from contextlib import asynccontextmanager
from dataclasses import asdict

import aiohttp
import pytest
import uvicorn
from aiohttp import web

from gittensor.controller.reconcile import InstanceRecord
from gittensor.controller.registry import Registry, make_entry, sign_bytes
from gittensor.gateway.app import Gateway, GatewayConfig, build_app
from gittensor.gateway.table import InstanceTable

KEY = 'test-gateway-key'
AUTH = {'X-GT-Gateway-Key': KEY}
IMAGE = 'entrius/fake-runtime@sha256:' + 'cd' * 32
NAME = 'gt-test'
RUNTIME_ID = 'fake-org/fake-model-27b'
OPENAI_ROUTES = [
    {'path': '/v1/chat/completions', 'method': 'POST', 'stream': True},
    {'path': '/v1/models', 'method': 'GET'},
]


def manifest_doc(name=NAME, front_door='gateway-openai', concurrency=4, routes=None) -> dict:
    return {
        'name': name,
        'version': 1,
        'runtime': 'fake',
        'image': IMAGE,
        'placement': {'gpu_types': {'include': ['RTX5090']}, 'cards_per_instance': 1, 'min_vram_gb': 1, 'max_load_s': 60},
        'health': {'http': {'path': '/v1/models', 'port': 8080, 'expect_status': 200}, 'interval_s': 30, 'failure_threshold': 3},
        'front_door': {'type': front_door, 'port': 8080, 'concurrency': concurrency, 'routes': routes or OPENAI_ROUTES},
        'drain': {'type': 'requests', 'max_s': 15},
    }  # fmt: skip


class FakeRuntime:
    """An OpenAI runtime on one instance's port. ``received`` is every request as it arrived (path, raw body).
    ``hold`` parks each completion until set (a busy instance); ``after_first`` parks a stream after its first chunk;
    ``error`` answers every completion with ``(status, body)``."""

    def __init__(self, model_id=RUNTIME_ID, usage=True, max_output_tokens: int | None = 32768):
        self.model_id, self.usage, self.max_output_tokens = model_id, usage, max_output_tokens
        self.received: list[dict] = []
        self.models_read = 0
        self.sent: list[bytes] = []
        self.hold: asyncio.Event | None = None
        self.after_first: asyncio.Event | None = None
        self.error: tuple[int, dict] | None = None
        self.port = 0
        self._runner: web.AppRunner | None = None

    def chunks(self) -> list[bytes]:
        head = {'id': 'chatcmpl-1', 'object': 'chat.completion.chunk', 'model': self.model_id}
        call = {
            'index': 0,
            'id': 'call_1',
            'type': 'function',
            'function': {'name': 'multiply', 'arguments': '{"a":17,"b":23}'},
        }
        deltas = [
            ({'role': 'assistant'}, None),
            ({'content': 'Let me '}, None),
            ({'content': 'call it.'}, None),
            ({'tool_calls': [call]}, None),
            ({}, 'tool_calls'),
        ]  # fmt: skip
        events = [{**head, 'choices': [{'index': 0, 'delta': d, 'finish_reason': f}]} for d, f in deltas]
        if self.usage:
            events.append(
                {**head, 'choices': [], 'usage': {'prompt_tokens': 42, 'completion_tokens': 9, 'total_tokens': 51}}
            )
        return [b'data: ' + json.dumps(e).encode() + b'\n\n' for e in events] + [b'data: [DONE]\n\n']

    async def _models(self, request):
        self.models_read += 1
        model = {
            'id': self.model_id,
            'object': 'model',
            'owned_by': 'fake',
            'created': 0,
            'context_length': 65536,
            'capabilities': {'tools': True, 'vision': {'image': True, 'video': False}},
        }
        if self.max_output_tokens is not None:
            model['max_output_tokens'] = self.max_output_tokens
        return web.json_response({'object': 'list', 'data': [model]})

    async def _completions(self, request):
        raw = await request.read()
        self.received.append({'path': request.path, 'body': raw})
        if self.hold is not None:
            await self.hold.wait()
        if self.error:
            return web.json_response(self.error[1], status=self.error[0])
        if json.loads(raw).get('stream'):
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            for i, chunk in enumerate(self.chunks()):
                await response.write(chunk)
                self.sent.append(chunk)
                if i == 0 and self.after_first is not None:
                    await self.after_first.wait()
                await asyncio.sleep(0.005)
            await response.write_eof()
            return response
        call = {'id': 'call_1', 'type': 'function', 'function': {'name': 'multiply', 'arguments': '{"a":17,"b":23}'}}
        answer = {
            'id': 'chatcmpl-1',
            'object': 'chat.completion',
            'model': self.model_id,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': None, 'tool_calls': [call]}, 'finish_reason': 'tool_calls'}],
        }  # fmt: skip
        if self.usage:
            answer['usage'] = {'prompt_tokens': 42, 'completion_tokens': 9, 'total_tokens': 51}
        return web.json_response(answer)

    async def _echo(self, request):
        raw = await request.read()
        self.received.append({'path': request.path, 'body': raw})
        return web.Response(body=raw, content_type='application/octet-stream')

    async def start(self):
        app = web.Application()
        app.router.add_get('/v1/models', self._models)
        app.router.add_post('/v1/chat/completions', self._completions)
        app.router.add_post('/v1/completions', self._completions)
        app.router.add_post('/echo', self._echo)
        self._runner = runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        self.port = runner.addresses[0][1]

    async def stop(self):
        for event in (self.hold, self.after_first):
            if event is not None:
                event.set()
        if self._runner is not None:
            await self._runner.cleanup()


@asynccontextmanager
async def runtimes(*fakes: FakeRuntime):
    for fake in fakes:
        await fake.start()
    try:
        yield fakes
    finally:
        for fake in fakes:
            await fake.stop()


def keypair(tmp_path, name='release'):
    key = tmp_path / name
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'test-release', '-f', str(key)], check=True)
    return key, (tmp_path / f'{name}.pub').read_text().strip()


class World:
    """The controller's state directory as the gateway sees it."""

    def __init__(self, tmp_path):
        self.root = tmp_path / 'state'
        self.root.mkdir()
        self.key, self.pubkey = keypair(tmp_path)
        self.registry = Registry(self.root / 'registry', self.pubkey)
        self.records: dict[str, dict] = {}
        self.tunnels: dict[str, dict] = {}
        self.written_at: float | None = None  # None: now, on every save

    def bless(self, doc: dict | None = None) -> str:
        verified = make_entry(doc or manifest_doc(), now=1_757_000_000)
        self.registry.write(verified, sign_bytes(verified.entry.canonical_bytes(), self.key))
        return verified.entry_id

    def place(
        self, instance_id: str, entry: str, port: int, healthy=True, draining=False, tunnel=True, record_port=None
    ) -> None:
        """An instance whose tunnel (``tunnel``: up) ends at ``port``. The record's own port is ``record_port``, by
        default one nothing listens on: a request that reaches the runtime went through the tunnel."""
        record = InstanceRecord(
            id=instance_id,
            entry=entry,
            box='hk-test',
            uuid=f'GPU-{instance_id}',
            container_id='c' * 64,
            host='127.0.0.1',
            port=record_port if record_port is not None else dead_port(),
            host_port=8080,
            healthy=healthy,
            draining=draining,
        )
        self.records[instance_id] = asdict(record)
        self.tunnels[instance_id] = {
            'box': 'hk-test', 'host': '127.0.0.1', 'port': port, 'up': tunnel, 'since': 0.0, 'error': '' if tunnel else 'x',
        }  # fmt: skip
        self.save()

    def update(self, instance_id: str, **fields) -> None:
        self.records[instance_id].update(fields)
        self.save()

    def tunnel(self, instance_id: str, **fields) -> None:
        self.tunnels[instance_id].update(fields)
        self.save()

    def save(self) -> None:
        tmp = self.root / 'instances.json.tmp'
        tmp.write_text(json.dumps(self.records, indent=1))
        tmp.replace(self.root / 'instances.json')
        self.save_tunnels()

    def save_tunnels(self) -> None:
        written_at = time.time() if self.written_at is None else self.written_at
        self._tunnels_doc = {'schema': 1, 'written_at': written_at, 'listen_host': '127.0.0.1', 'tunnels': self.tunnels}
        tmp = self.root / 'tunnels.json.tmp'
        tmp.write_text(json.dumps(self._tunnels_doc, indent=1))
        tmp.replace(self.root / 'tunnels.json')

    def tunnels_doc(self) -> dict:
        """What the last save wrote."""
        return json.loads(json.dumps(self._tunnels_doc))


def dead_port() -> int:
    """A loopback port nothing listens on."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


@asynccontextmanager
async def gateway(world: World, sink=None, allow_direct=False, **config):
    """The gateway app on a real loopback socket (lifespan on: the first table read happens before it serves)."""
    config.setdefault('refresh_s', 0.05)
    table = InstanceTable(world.root, world.registry, allow_direct=allow_direct)
    gw = Gateway(GatewayConfig(key=KEY, **config), table, sink=sink)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(build_app(gw), log_level='warning', lifespan='on'))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        async with aiohttp.ClientSession(base_url=f'http://127.0.0.1:{sock.getsockname()[1]}') as client:
            yield gw, client
    finally:
        server.should_exit = True
        await task
        sock.close()


async def until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('condition not met in time')
        await asyncio.sleep(0.02)


async def post(client, body=None, path='/v1/chat/completions', headers=AUTH, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    async with client.post(path, data=data, headers={**headers, 'Content-Type': 'application/json'}) as resp:
        return resp.status, await resp.read(), resp.headers.copy()  # case-insensitive
