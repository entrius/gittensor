# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The gateway service (vault ``26`` §2, ``25`` "Front door types", ``23`` §5).

    POST /v1/chat/completions, /v1/completions   gateway-openai entries, routed by ``model``
    GET  /v1/models                              each entry's runtime /v1/models under its manifest name + override
    ANY  /http/<name><route>                     http entries, declared routes only, passed through
    GET  /healthz                                routable instances per entry, tunnels, per-instance totals (the only
                                                 route without the key)
    GET  /metrics                                plain-text counters per entry and instance

Every request but ``GET /healthz`` carries ``X-GT-Gateway-Key``: das sends it; user keys, quota and billing are das's.
A request takes a slot on a routable instance, least loaded first; nothing free is a 429 at once, never a queue. The
body goes on as sent (no output cap of ours: ``limits.py``), save ``model`` set to the runtime's own id when the
client named the entry; the runtime's answer comes back unchanged: SSE relayed chunk by chunk, errors with their
body. One
JSON usage line per request goes to stdout with the manifest expected-profile signals (``23`` §5), recorded here and
judged by no one here.

The gateway reaches an instance only through its tunnel (``table.py``): the keeper's SSH connection to the box, HTTP
inside it. Every request, and the ``/v1/models`` fetch, goes to ``Instance.base_url``.

Per instance, since the gateway started (``started_at``), ``/healthz`` carries what it served (``served``): requests,
completion tokens as the runtime's ``usage`` gave them, and the requests whose completion tokens it did not learn (no
``usage``, a client that left mid-stream, an upstream error) with what each could have made at most: its own
``max_tokens``, else the runtime's output ceiling. The controller's lease accounting check compares these with the
runtime's own counters (``controller/usage_check.py``). Beside them, the median decode rate over requests that ran
alone on their instance, recorded as evidence.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
from collections import Counter, deque
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from gittensor.controller.checks import config as cfg
from gittensor.controller.tunnels_file import TUNNELS_FILE
from gittensor.controller.usage_check import median, output_ceiling, request_allowance
from gittensor.gateway.limits import RequestRefused, enforce_openai_limits, parse_object
from gittensor.gateway.table import Instance, InstanceTable

KEY_HEADER = 'X-GT-Gateway-Key'
SSE_DONE = b'data: [DONE]\n\n'
CLIENT_CLOSED = 499  # nginx's code for a client that left before the answer finished
_HTTP_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
SERVED_KEEP_S = 3_600.0  # an instance's totals are kept this long after it left the table, then dropped

log = logging.getLogger('gittensor.gateway')


@dataclass(frozen=True)
class GatewayConfig:
    key: str
    refresh_s: float = 3.0
    request_timeout_s: float = 600.0
    connect_timeout_s: float = 10.0
    models_timeout_s: float = 3.0
    max_body_bytes: int = 16 * 1024 * 1024


@dataclass
class Usage:
    """The one line per request. Tokens come from the runtime's ``usage`` or are null, never estimated."""

    ts: float
    instance: str | None = None
    entry: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    ttft_ms: float | None = None
    total_ms: float | None = None
    decode_tps: float | None = None
    status: int = 0
    finish_reason: str | None = None
    stream: bool = False


@dataclass
class ServedTotals:
    """What the gateway sent one instance since it started. A request's completion tokens are the runtime's own
    ``usage``; one whose count was not learned is *unaccounted* and adds the most it could have made."""

    entry: str
    requests: int = 0
    completion_tokens: int = 0
    unaccounted_requests: int = 0
    unaccounted_allowance_tokens: int = 0
    decode_tps_alone: deque = field(default_factory=lambda: deque(maxlen=cfg.DECODE_TPS_WINDOW))
    last_at: float = 0.0

    def count(self, completion_tokens: int | None, allowance: int, decode_tps_alone: float | None, now: float) -> None:
        self.requests += 1
        if completion_tokens is None:
            self.unaccounted_requests += 1
            self.unaccounted_allowance_tokens += allowance
        else:
            self.completion_tokens += completion_tokens
        if decode_tps_alone is not None:
            self.decode_tps_alone.append(decode_tps_alone)
        self.last_at = now

    def as_dict(self) -> dict[str, Any]:
        return {
            'requests': self.requests,
            'completion_tokens': self.completion_tokens,
            'unaccounted_requests': self.unaccounted_requests,
            'unaccounted_allowance_tokens': self.unaccounted_allowance_tokens,
            'decode_tps_alone_p50': median(list(self.decode_tps_alone)),
            'decode_tps_alone_n': len(self.decode_tps_alone),
        }


def _stdout(line: str) -> None:
    print(line, flush=True)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class Metrics:
    def __init__(self) -> None:
        self.requests: Counter[tuple[str, str]] = Counter()
        self.errors: Counter[tuple[str, str]] = Counter()
        self.capacity: Counter[str] = Counter()

    def render(self, table: InstanceTable, served: dict[str, ServedTotals] | None = None) -> str:
        lines = []
        for (entry, instance), n in sorted(self.requests.items()):
            lines.append(f'gt_gateway_requests_total{{entry="{entry}",instance="{instance}"}} {n}')
        for instance, totals in sorted((served or {}).items(), key=lambda kv: (kv[1].entry, kv[0])):
            lines.append(
                f'gt_gateway_completion_tokens_total{{entry="{totals.entry}",instance="{instance}"}} '
                f'{totals.completion_tokens}'
            )
        for entry, n in sorted(self.capacity.items()):
            lines.append(f'gt_gateway_capacity_429_total{{entry="{entry}"}} {n}')
        for (entry, instance), n in sorted(self.errors.items()):
            lines.append(f'gt_gateway_errors_total{{entry="{entry}",instance="{instance}"}} {n}')
        for i in sorted(table.instances.values(), key=lambda i: (i.entry, i.id)):
            lines.append(f'gt_gateway_in_flight{{entry="{i.entry}",instance="{i.id}"}} {table.in_flight.get(i.id, 0)}')
        for entry, n in table.routable_counts().items():
            lines.append(f'gt_gateway_routable_instances{{entry="{entry}"}} {n}')
        for i in sorted(table.instances.values(), key=lambda i: (i.entry, i.id)):
            lines.append(f'gt_gateway_tunnel_up{{entry="{i.entry}",instance="{i.id}"}} {int(i.id in table.tunnels.up)}')
        lines.append(f'gt_gateway_tunnels_fresh {int(table.tunnels.fresh)}')
        return '\n'.join(lines) + '\n'


class StreamWatch:
    """Reads the relayed SSE bytes for the usage line and never changes them. Events split as phase 0's
    ``SSEParser`` does (copied, not imported)."""

    def __init__(self) -> None:
        self._buf = b''
        self.done = False
        self.first_token_at: float | None = None
        self.last_token_at: float | None = None
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] = {}

    def feed(self, chunk: bytes, now: float) -> None:
        self._buf = (self._buf + chunk).replace(b'\r\n', b'\n')
        while b'\n\n' in self._buf:
            raw, self._buf = self._buf.split(b'\n\n', 1)
            for line in raw.split(b'\n'):
                if not line.startswith(b'data:'):
                    continue
                data = line[5:].strip()
                if data == b'[DONE]':
                    self.done = True
                    continue
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self._observe(event, now)

    def _observe(self, event: dict[str, Any], now: float) -> None:
        if isinstance(event.get('usage'), dict):
            self.usage = event['usage']
        for choice in event.get('choices') or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get('delta')
            delta = delta if isinstance(delta, dict) else {}
            if choice.get('text') or any(delta.get(k) for k in ('content', 'reasoning_content', 'tool_calls')):
                self.first_token_at = self.first_token_at if self.first_token_at is not None else now
                self.last_token_at = now
            if choice.get('finish_reason'):
                self.finish_reason = str(choice['finish_reason'])


def _fill_from_stream(usage: Usage, watch: StreamWatch, started: float) -> None:
    usage.prompt_tokens = _int(watch.usage.get('prompt_tokens'))
    usage.completion_tokens = _int(watch.usage.get('completion_tokens'))
    usage.finish_reason = watch.finish_reason
    first, last = watch.first_token_at, watch.last_token_at
    if first is not None:
        usage.ttft_ms = round((first - started) * 1000.0, 1)
    tokens = usage.completion_tokens
    if tokens and tokens > 1 and first is not None and last is not None and last > first:
        usage.decode_tps = round((tokens - 1) / (last - first), 2)


def _fill_from_json(usage: Usage, data: bytes) -> None:
    try:
        doc = json.loads(data)
    except ValueError:
        return
    if not isinstance(doc, dict):
        return
    counts = doc.get('usage')
    counts = counts if isinstance(counts, dict) else {}
    usage.prompt_tokens = _int(counts.get('prompt_tokens'))
    usage.completion_tokens = _int(counts.get('completion_tokens'))
    choices = doc.get('choices')
    if isinstance(choices, list) and choices and isinstance(choices[0], dict) and choices[0].get('finish_reason'):
        usage.finish_reason = str(choices[0]['finish_reason'])


def _error(status: int, error_type: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({'error': {'type': error_type, 'message': message}}, status_code=status, headers=headers)


async def read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get('content-length', '')
    if declared.isdigit() and int(declared) > limit:
        raise RequestRefused(413, f'body over {limit} bytes')
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise RequestRefused(413, f'body over {limit} bytes')
        chunks.append(chunk)
    return b''.join(chunks)


class RelayResponse(StreamingResponse):
    """A streaming response whose ``on_close`` runs however it ends: finished, client gone, or cancelled. The slot is
    released there, never in the body generator, which a disconnect can leave suspended."""

    def __init__(self, content: AsyncIterator[bytes], on_close: Callable[[], None], **kwargs: Any):
        super().__init__(content, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._on_close()
            with contextlib.suppress(Exception):
                await self.body_iterator.aclose()  # type: ignore[union-attr]


class RequireKey:
    """ASGI middleware: ``X-GT-Gateway-Key`` on everything but ``GET /healthz``."""

    def __init__(self, app, key: str):
        self.app, self.key = app, key.encode()

    async def __call__(self, scope, receive, send) -> None:
        if scope['type'] != 'http' or (scope['path'] == '/healthz' and scope['method'] in ('GET', 'HEAD')):
            return await self.app(scope, receive, send)
        given = dict(scope.get('headers') or []).get(KEY_HEADER.lower().encode(), b'')
        if not hmac.compare_digest(given, self.key):
            response = _error(401, 'unauthorized', f'missing or wrong {KEY_HEADER}')
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)


class Gateway:
    def __init__(
        self,
        config: GatewayConfig,
        table: InstanceTable,
        sink: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.config, self.table = config, table
        self.metrics = Metrics()
        self.sink = sink or _stdout
        self.clock, self.wall = clock, wall
        self.session: aiohttp.ClientSession | None = None
        self._refresher: asyncio.Task | None = None
        self.started_at = wall()
        self.served: dict[str, ServedTotals] = {}  # instance id -> totals since started_at
        self._running: dict[str, list[dict[str, bool]]] = {}  # instance id -> its requests in flight: ran alone so far?

    @property
    def client(self) -> aiohttp.ClientSession:
        if self.session is None:
            raise RuntimeError('the gateway is not started')
        return self.session

    # -- lifecycle ----------------------------------------------------------------------------------------------------

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_s, sock_connect=self.config.connect_timeout_s)
        self.session = aiohttp.ClientSession(timeout=timeout)
        await self.refresh()
        self._refresher = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        if self._refresher is not None:
            self._refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresher
        if self.session is not None:
            await self.session.close()

    async def refresh(self) -> None:
        loaded = await asyncio.to_thread(self.table.read)
        tunnels_before = self.table.tunnels.error
        self.table.apply(loaded, self.wall())
        self._prune_served()
        if loaded.error:
            log.warning('refresh: %s', loaded.error)
        if loaded.tunnels.error != tunnels_before:  # once per change: a missing keeper is logged, not every refresh
            if loaded.tunnels.error:
                log.warning('refresh: %s', loaded.tunnels.error)
            else:
                log.info('refresh: %s read, %d tunnels up', TUNNELS_FILE, len(loaded.tunnels.up))
        await self._refresh_models()

    def _prune_served(self) -> None:
        """Drop the totals of an instance ``SERVED_KEEP_S`` after it left the table (a lease that ended long ago)."""
        now = self.wall()
        for instance_id, totals in list(self.served.items()):
            gone = instance_id not in self.table.instances and not self.table.in_flight.get(instance_id)
            if gone and now - totals.last_at > SERVED_KEEP_S:
                del self.served[instance_id]

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.refresh_s)
            try:
                await self.refresh()
            except Exception:
                log.exception('refresh failed; keeping the previous table')

    async def _refresh_models(self) -> None:
        """Each gateway-openai entry's ``/v1/models`` from one routable instance; a failed fetch keeps the cache."""
        timeout = aiohttp.ClientTimeout(total=self.config.models_timeout_s)

        async def fetch(entry_id: str, instances: list[Instance]) -> None:
            instance = self.table.rng.choice(instances)
            try:
                async with self.client.get(instance.base_url + '/v1/models', timeout=timeout) as resp:
                    doc = await resp.json(content_type=None) if resp.status == 200 else None
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return
            data = doc.get('data') if isinstance(doc, dict) else None
            if isinstance(data, list):
                self.table.runtime_models[entry_id] = data

        await asyncio.gather(*(fetch(e, i) for e, i in self.table.models_source().items()))

    # -- the usage line -----------------------------------------------------------------------------------------------

    def emit(self, usage: Usage) -> None:
        try:
            self.sink(json.dumps(asdict(usage), separators=(',', ':')))
        except Exception:
            log.exception('usage line not written')

    def _refused(self, usage: Usage, started: float, response: JSONResponse) -> JSONResponse:
        usage.status = response.status_code
        usage.total_ms = round((self.clock() - started) * 1000.0, 1)
        self.emit(usage)
        return response

    def _no_capacity(self, usage: Usage, started: float, candidates: list[Instance]) -> JSONResponse:
        entries = sorted({i.entry for i in candidates})
        for entry in entries:
            self.metrics.capacity[entry] += 1
        usage.entry = entries[0] if len(entries) == 1 else None
        response = _error(429, 'capacity', 'no free slot on a healthy instance; retry', {'Retry-After': '1'})
        return self._refused(usage, started, response)

    # -- handlers -----------------------------------------------------------------------------------------------------

    async def openai(self, request: Request) -> Response:
        started, path = self.clock(), request.url.path
        usage = Usage(ts=self.wall())
        try:
            raw = await read_body(request, self.config.max_body_bytes)
            body = parse_object(raw)
            model = body.get('model')
            usage.model = model if isinstance(model, str) else None
            usage.stream = body.get('stream') is True
            if not isinstance(model, str) or not model:
                raise RequestRefused(400, 'model is required: see GET /v1/models')
            enforce_openai_limits(body, path)
            candidates = self.table.openai_candidates(model)
            if not candidates:
                raise RequestRefused(404, f'model {model!r} is not served: see GET /v1/models', 'model_not_found')
        except RequestRefused as e:
            return self._refused(usage, started, _error(e.status, e.error_type, e.message))
        instance = self.table.acquire(candidates)
        if instance is None:
            return self._no_capacity(usage, started, candidates)
        runtime_model = self.table.runtime_model_for(instance, model)
        if runtime_model:  # the one rewrite: the entry's name -> the runtime's own id; otherwise the exact bytes
            body['model'] = runtime_model
        payload = raw if not runtime_model else json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
        headers = {'Content-Type': 'application/json', 'Accept': request.headers.get('accept', '*/*')}
        allowance = request_allowance(body, output_ceiling(instance.manifest))
        return await self._forward(instance, 'POST', path, payload, headers, usage, started, 'sse', allowance)

    async def http(self, request: Request, name: str, path: str) -> Response:
        started, route, method = self.clock(), '/' + path, request.method.upper()
        usage = Usage(ts=self.wall(), model=name)
        try:
            candidates = self.table.http_candidates(name)
            if not candidates:
                raise RequestRefused(404, f'no http workload named {name!r}', 'not_found')
            declared = [i for i in candidates if i.declares(method, route)]
            if not declared and any(i.manifest for i in candidates):
                raise RequestRefused(404, f'{method} {route} is not a declared route of {name!r}', 'not_found')
            raw = await read_body(request, self.config.max_body_bytes)
        except RequestRefused as e:
            return self._refused(usage, started, _error(e.status, e.error_type, e.message))
        instance = self.table.acquire(declared)
        if instance is None:
            return self._no_capacity(usage, started, candidates)
        routes = instance.manifest.front_door.routes if instance.manifest else ()  # declared: always verified
        streams = any(r.stream for r in routes if r.path == route and r.method.upper() == method)
        usage.stream = streams
        target = route + (f'?{request.url.query}' if request.url.query else '')
        headers = {k: v for k, v in request.headers.items() if k.lower() in ('content-type', 'accept')}
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        allowance = request_allowance(parsed if isinstance(parsed, dict) else None, output_ceiling(instance.manifest))
        return await self._forward(
            instance, method, target, raw or None, headers, usage, started, 'raw' if streams else '', allowance
        )

    async def _forward(
        self,
        instance: Instance,
        method: str,
        target: str,
        payload: bytes | None,
        headers: dict[str, str],
        usage: Usage,
        started: float,
        relay: str,  # 'sse': relay an event-stream answer and read it for usage; 'raw': relay bytes; '': whole
        allowance: int = cfg.RUNTIME_OUTPUT_CEILING_TOKENS,  # the most this request can make, if its count is not learned
    ) -> Response:
        key = (instance.entry, instance.id)
        self.metrics.requests[key] += 1
        usage.instance, usage.entry = instance.id, instance.entry
        done = False
        peers = self._running.setdefault(instance.id, [])
        mine = {'alone': not peers}
        for peer in peers:
            peer['alone'] = False
        peers.append(mine)

        def finish(status: int, failed: bool = False) -> None:
            nonlocal done
            if done:
                return
            done = True
            self.table.release(instance.id)
            usage.status = status
            usage.total_ms = round((self.clock() - started) * 1000.0, 1)
            if failed or status >= 500:
                self.metrics.errors[key] += 1
            self._count(instance, usage, allowance, mine)
            self.emit(usage)

        try:
            upstream = await self.client.request(method, instance.base_url + target, data=payload, headers=headers)
        except TimeoutError:
            finish(504, True)
            return _error(504, 'upstream', f'instance {instance.id} did not answer in time')
        except aiohttp.ClientError as e:
            finish(502, True)
            return _error(502, 'upstream', f'instance {instance.id} unreachable: {type(e).__name__}')
        except BaseException:
            finish(CLIENT_CLOSED)
            raise

        content_type = upstream.headers.get('Content-Type', 'application/octet-stream')
        out_headers = {'content-type': content_type, 'X-GT-Instance': instance.id, 'X-GT-Entry': instance.entry}
        if 'Retry-After' in upstream.headers:
            out_headers['Retry-After'] = upstream.headers['Retry-After']
        is_sse = content_type.lower().startswith('text/event-stream')

        if upstream.status == 200 and ((relay == 'sse' and is_sse) or relay == 'raw'):
            watch = StreamWatch() if relay == 'sse' else None
            state = {'ended': False, 'broken': ''}

            async def chunks() -> AsyncIterator[bytes]:
                try:
                    async for chunk in upstream.content.iter_any():
                        if watch is not None:
                            watch.feed(chunk, self.clock())
                        yield chunk
                    if watch is not None and not watch.done:
                        state['broken'] = 'the instance ended the stream before [DONE]'
                except (aiohttp.ClientError, TimeoutError) as e:
                    state['broken'] = f'the instance stream failed: {type(e).__name__}'
                if state['broken'] and watch is not None:
                    error = {'error': {'type': 'upstream', 'message': state['broken']}}
                    yield b'data: ' + json.dumps(error).encode() + b'\n\n' + SSE_DONE
                state['ended'] = True

            def on_close() -> None:
                upstream.close()
                usage.stream = True
                if watch is not None:
                    _fill_from_stream(usage, watch, started)
                if state['broken']:
                    finish(502, True)
                else:
                    finish(200 if state['ended'] else CLIENT_CLOSED)

            out_headers.update({'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
            return RelayResponse(chunks(), on_close, status_code=200, headers=out_headers)

        try:
            data = await upstream.read()
        except TimeoutError:
            finish(504, True)
            return _error(504, 'upstream', f'instance {instance.id} did not answer in time')
        except aiohttp.ClientError as e:
            finish(502, True)
            return _error(502, 'upstream', f'instance {instance.id} failed mid-answer: {type(e).__name__}')
        except BaseException:
            finish(CLIENT_CLOSED)
            raise
        finally:
            upstream.release()
        if relay == 'sse' and upstream.status == 200:
            _fill_from_json(usage, data)
        finish(upstream.status)
        return Response(content=data, status_code=upstream.status, headers=out_headers)

    def _count(self, instance: Instance, usage: Usage, allowance: int, mine: dict[str, bool]) -> None:
        running = self._running.get(instance.id, [])
        if any(r is mine for r in running):
            running.remove(next(r for r in running if r is mine))
        if not running:
            self._running.pop(instance.id, None)
        totals = self.served.setdefault(instance.id, ServedTotals(instance.entry))
        alone = usage.decode_tps if mine['alone'] else None
        totals.count(usage.completion_tokens, allowance, alone, self.wall())


def build_app(gateway: Gateway) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await gateway.start()  # the first table read finishes before the first request is served
        try:
            yield
        finally:
            await gateway.stop()

    app = FastAPI(title='Gittensor compute gateway', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(RequireKey, key=gateway.config.key)

    @app.get('/healthz')
    async def healthz():
        table = gateway.table
        return {
            'status': 'ok',
            'routable': table.routable_counts(),
            'instances': len(table.instances),
            # requests being served right now, per instance: the controller waits for a draining instance's to reach
            # zero before it stops the container (no stream cut by a rotation)
            'in_flight': {i: n for i, n in table.in_flight.items() if n > 0},
            'refreshed_at': table.loaded_at,
            'error': table.last_error,
            'tunnels': table.tunnels.summary(),
            # since started_at, per instance: what was sent and what each request whose count was not learned could
            # have made at most (the controller's lease accounting check reads these)
            'started_at': gateway.started_at,
            'served': {i: t.as_dict() for i, t in sorted(gateway.served.items())},
        }

    @app.get('/metrics')
    async def metrics():
        return PlainTextResponse(gateway.metrics.render(gateway.table, gateway.served))

    @app.get('/v1/models')
    async def models():
        return {'object': 'list', 'data': gateway.table.model_objects()}

    @app.post('/v1/chat/completions')
    async def chat_completions(request: Request):
        return await gateway.openai(request)

    @app.post('/v1/completions')
    async def completions(request: Request):
        return await gateway.openai(request)

    @app.api_route('/http/{name}/{path:path}', methods=_HTTP_METHODS)
    async def http_passthrough(request: Request, name: str, path: str):
        return await gateway.http(request, name, path)

    return app
