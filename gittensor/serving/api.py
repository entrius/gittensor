# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Validator-hosted OpenAI-compatible inference API (sub-subnet B beta).

    POST /v1/chat/completions   Authorization: Bearer <key>

The validator serves this; each request is forwarded to the READY miner with
the fewest in-flight requests and, once served, handed to the audit loop to be
verified against the reference — served traffic is the only audit there is.
Keys are a static list in ``SERVING_API_KEYS`` for now (the product front door
owns users/credits). Requests from ``SERVING_BASELINE_API_KEYS`` may be routed
to probation (not yet READY) miners so new miners can earn a window; user keys
only ever reach READY miners. Off unless keys are set. Binds loopback by
default; put it behind the host reverse proxy / TLS like any other service.

No queue, at either layer: a saturated miner refuses "busy" instead of
queueing (runtime contract R6 at the axon), the gateway retries the next
READY miner, and with no capacity left — none READY, or everyone tried
refused — it returns 429 so rejected demand stays visible.

    OPENAI_BASE_URL=http://<host>:8790/v1 OPENAI_API_KEY=<key> ...
"""

import asyncio
import threading
import time
import uuid
from typing import Awaitable, Callable, Dict, List, Optional, Set

import bittensor as bt
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gittensor.constants import (
    SERVING_CONTEXT_TOKENS_FALLBACK,
    SERVING_GATEWAY_BUSY_RETRIES,
    SERVING_MAX_PROMPT_CHARS,
    SERVING_MAX_TOKENS,
)
from gittensor.serving.loadout import ServingLoadout, ServingRelease
from gittensor.serving.state import (
    ReadyMiner,
    RequestRecord,
    ServedRequest,
    ServingState,
    finite_or_none,
    is_busy_detail,
    prompt_token_estimate,
)
from gittensor.serving.stream import SSE_DONE, Event, consume_stream, sse_event
from gittensor.synapses import InferenceSynapse

_END = object()  # end-of-stream marker on the relay queue

_bearer = HTTPBearer(auto_error=False)


def parse_api_keys(raw: Optional[str]) -> Set[str]:
    """Comma-separated bearer keys."""
    return {k.strip() for k in (raw or '').split(',') if k.strip()}


def build_app(
    state: ServingState,
    loadout: ServingLoadout,
    api_keys: Set[str],
    dendrite_factory,
    request_timeout: float,
    baseline_keys: Optional[Set[str]] = None,
) -> FastAPI:
    baseline = set(baseline_keys or ())
    primary = loadout.primary
    app = FastAPI(title='Gittensor Serving API', version='0.1.0-beta')
    dendrite_holder: Dict[str, bt.Dendrite] = {}

    def get_dendrite() -> bt.Dendrite:
        # Created lazily on the gateway's own event loop.
        if 'd' not in dendrite_holder:
            dendrite_holder['d'] = dendrite_factory()
        return dendrite_holder['d']

    def require_key(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> str:
        if creds is None or creds.credentials not in api_keys:
            raise HTTPException(status_code=401, detail='invalid api key')
        return creds.credentials

    @app.on_event('shutdown')
    async def _close_dendrite():
        dendrite = dendrite_holder.pop('d', None)
        if dendrite is not None:
            await dendrite.aclose_session()

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'ready_miners': len(state.ready_miners())}

    @app.get('/v1/models')
    async def models(_: str = Depends(require_key)):
        return {
            'object': 'list',
            'data': [
                {
                    'id': release.release_id,
                    'object': 'model',
                    'owned_by': 'gittensor',
                    # release identity (contract P2), informational: READY miners are verified by greedy conformance
                    # against a reference running this release and by the attestation digest, not by these strings
                    'model_id': release.model_id,
                    'runtime_pin': release.runtime_pin,
                    'model_sha256': release.model_sha256,
                    # OpenRouter's fields: agent harnesses size their context compaction from these, so a client
                    # that reads them never has to hit the 400 context_length_exceeded
                    'context_length': release.context_tokens or SERVING_CONTEXT_TOKENS_FALLBACK,
                    'max_output_tokens': SERVING_MAX_TOKENS,
                }
                for release in loadout.releases
            ],
        }

    @app.get('/v1/serving/status')
    async def status(_: str = Depends(require_key)):
        snap = state.snapshot()
        snap['model_id'] = primary.model_id
        snap['release_id'] = primary.release_id
        snap['releases'] = [r.release_id for r in loadout.releases]
        snap['recent'] = [r.__dict__ for r in state.recent(50)]
        return snap

    @app.post('/v1/chat/completions')
    async def chat_completions(request: Request, key: str = Depends(require_key)):
        body = await request.json()
        messages = body.get('messages')
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail='messages must be a non-empty list')
        if not all(
            isinstance(m, dict) and isinstance(m.get('role'), str) and isinstance(m.get('content'), str)
            for m in messages
        ):
            raise HTTPException(
                status_code=400, detail='each message needs string role and content (no array content yet)'
            )
        if body.get('n', 1) != 1:
            raise HTTPException(status_code=400, detail='n must be 1')
        if sum(len(m['content']) + len(m['role']) for m in messages) > SERVING_MAX_PROMPT_CHARS:
            raise HTTPException(status_code=413, detail=f'messages exceed {SERVING_MAX_PROMPT_CHARS} characters')
        wanted = body.get('model')
        try:  # `model` = a release_id (or a model_id: its first release); absent -> the primary release
            release = loadout.get(str(wanted)) if wanted else primary
        except KeyError:
            raise HTTPException(status_code=404, detail=f'model {wanted!r} is not served; see /v1/models')
        raw_max = body.get('max_tokens', body.get('max_completion_tokens'))
        try:
            max_tokens = int(raw_max) if raw_max is not None else release.max_tokens
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='max_tokens must be an integer')
        if max_tokens <= 0:
            raise HTTPException(status_code=400, detail='max_tokens must be positive')
        # The prompt's limit is the release's context window, not a character count. Estimated here (~4 characters
        # per token) and enforced exactly by the runtime; a prompt that cannot fit is OpenAI's 400 so agent SDKs
        # trim history and retry, and a completion that would overrun the window is clamped to what is left.
        context = release.context_tokens or SERVING_CONTEXT_TOKENS_FALLBACK
        prompt_estimate = prompt_token_estimate(messages)
        if prompt_estimate >= context:
            raise HTTPException(
                status_code=400,
                detail=f"context_length_exceeded: this model's maximum context length is {context} tokens, "
                f'and the messages alone are about {prompt_estimate} tokens',
            )
        max_tokens = min(max_tokens, SERVING_MAX_TOKENS, context - prompt_estimate)
        want_logprobs = bool(body.get('logprobs', False))
        want_stream = bool(body.get('stream', False))
        stream_options = body.get('stream_options')
        include_usage = isinstance(stream_options, dict) and bool(stream_options.get('include_usage'))

        request_id = f'chatcmpl-{uuid.uuid4().hex[:24]}'
        created = int(time.time())
        tried: Set[int] = set()  # uids that refused this request busy; the retry must land elsewhere

        for _ in range(1 + SERVING_GATEWAY_BUSY_RETRIES):
            picked = state.acquire(release.release_id, probation=key in baseline, exclude=tried)
            if picked is None:
                raise HTTPException(
                    status_code=429, detail='all serving capacity busy' if tried else 'no READY serving capacity'
                )
            miner = picked
            inflight = state.inflight().get(miner.uid, 1)
            state.charge_sent(miner.hotkey, max_tokens)
            start = time.monotonic()

            def finish(result: Optional[InferenceSynapse]) -> bool:
                state.release(miner.uid)
                ok = result is not None and result.completion is not None
                # The miner's stream end, not the moment the user finished reading it: a slow client must not read
                # as a slow card.
                latency_ms = finite_or_none(getattr(result, 'observed_latency_ms', None)) if result else None
                if latency_ms is None:
                    latency_ms = (time.monotonic() - start) * 1000.0
                state.enqueue_served(
                    ServedRequest(
                        ts=time.time(),
                        uid=miner.uid,
                        hotkey=miner.hotkey,
                        model_id=release.model_id,
                        release_id=release.release_id,
                        messages=messages,
                        ok=ok and (result.served_model_id == release.model_id if result else False),
                        latency_ms=latency_ms,
                        completion=result.completion if result else None,
                        tokens=list(result.tokens) if result and result.tokens else None,
                        token_ids=list(result.token_ids) if result and result.token_ids else None,
                        token_bytes=list(result.token_bytes) if result and result.token_bytes else None,
                        token_logprobs=list(result.token_logprobs) if result and result.token_logprobs else None,
                        detail=str(getattr(getattr(result, 'dendrite', None), 'status_message', None) or '')
                        if not ok
                        else '',
                        source='gateway',
                        ttft_ms=finite_or_none(getattr(result, 'observed_ttft_ms', None)) if result else None,
                        inflight=inflight,
                        max_tokens=max_tokens,
                        prompt_tokens=int((result.usage or {}).get('prompt_tokens') or 0) if result else 0,
                    )
                )
                state.record(
                    RequestRecord(
                        ts=time.time(),
                        kind='gateway',
                        uid=miner.uid,
                        ok=ok,
                        latency_ms=latency_ms,
                        completion_tokens=(result.usage or {}).get('completion_tokens', 0) if result else 0,
                        ttft_ms=result.ttft_ms if result else None,
                        decode_tps=result.decode_tps if result else None,
                        detail='' if ok else 'miner returned no completion',
                    )
                )
                return ok

            def failed() -> JSONResponse:
                return JSONResponse(
                    status_code=502, content={'error': {'message': 'serving miner failed', 'uid': miner.uid}}
                )

            def reshape(event: dict) -> dict:
                """Relay a miner chunk to the client under our request id, without logprobs unless asked for."""
                out = dict(event)
                out['id'] = request_id
                if not want_logprobs:
                    out['choices'] = [
                        {k: v for k, v in c.items() if k != 'logprobs'} for c in event.get('choices') or []
                    ]
                if 'usage' in event:
                    out['gittensor'] = {
                        'served_uid': miner.uid,
                        'latency_ms': round((time.monotonic() - start) * 1000, 1),
                    }
                return out

            if want_stream:
                queue: 'asyncio.Queue[object]' = asyncio.Queue()

                async def relay(event: Event, queue=queue) -> None:
                    await queue.put(event)

                async def run(miner=miner, relay=relay, queue=queue) -> Optional[InferenceSynapse]:
                    try:
                        return await _dispatch(
                            get_dendrite(), miner, messages, max_tokens, release, request_timeout, relay
                        )
                    finally:
                        await queue.put(_END)

                task = asyncio.create_task(run())

                async def outcome(task=task) -> Optional[InferenceSynapse]:
                    try:  # a stream the assembler could not fold is a miss, never a leaked in-flight slot
                        return await task
                    except Exception:
                        return None

                first = await queue.get()
                if first is _END or first is None:  # nothing streamed before the miner gave up
                    result = await outcome()
                    finish(result)
                    if _busy_refused(result):  # refused at capacity, nothing sent to the client: try elsewhere
                        tried.add(miner.uid)
                        continue
                    return failed()

                async def body_iter():
                    event = first
                    try:
                        while event is not _END:
                            if event is None:
                                yield SSE_DONE
                            # OpenAI emits the choices-less usage chunk only when stream_options asks for it.
                            elif include_usage or event.get('choices') or 'usage' not in event:  # type: ignore[union-attr]
                                yield sse_event(reshape(event))  # type: ignore[arg-type]
                            event = await queue.get()
                    finally:
                        finish(await outcome())

                return StreamingResponse(body_iter(), media_type='text/event-stream')

            try:
                result = await _dispatch(get_dendrite(), miner, messages, max_tokens, release, request_timeout)
            except Exception:
                result = None
            if _busy_refused(result):  # refused at capacity: try elsewhere
                finish(result)
                tried.add(miner.uid)
                continue
            if not finish(result) or result is None:
                return failed()

            choice: Dict = {
                'index': 0,
                'message': {'role': 'assistant', 'content': result.completion},
                'finish_reason': result.finish_reason or 'stop',
            }
            if want_logprobs and result.tokens and result.token_logprobs:
                choice['logprobs'] = {
                    'content': [{'token': t, 'logprob': lp} for t, lp in zip(result.tokens, result.token_logprobs)]
                }
            return {
                'id': request_id,
                'object': 'chat.completion',
                'created': created,
                'model': result.served_model_id or release.model_id,
                'choices': [choice],
                'usage': result.usage or {},
                'gittensor': {
                    'served_uid': miner.uid,
                    'latency_ms': round((time.monotonic() - start) * 1000.0, 1),
                    'ttft_ms': finite_or_none(result.ttft_ms),
                    'decode_tps': finite_or_none(result.decode_tps),
                },
            }

        raise HTTPException(status_code=429, detail='all serving capacity busy')

    return app


def _busy_refused(result: Optional[InferenceSynapse]) -> bool:
    """Did the miner refuse this dispatch at capacity? Nothing was served and the axon status says busy."""
    if result is None or result.completion is not None:
        return False
    return is_busy_detail(str(getattr(getattr(result, 'dendrite', None), 'status_message', None) or ''))


async def _dispatch(
    dendrite: bt.Dendrite,
    miner: ReadyMiner,
    messages: List[Dict[str, str]],
    max_tokens: int,
    release: ServingRelease,
    timeout: float,
    on_event: Optional[Callable[[Event], Awaitable[None]]] = None,
) -> InferenceSynapse:
    # logprobs always requested so organic traffic is indistinguishable from audits on the wire.
    synapse = InferenceSynapse(
        messages=messages,
        model_id=release.model_id,
        release_id=release.release_id,
        max_tokens=max_tokens,
        logprobs=True,
    )
    return await consume_stream(dendrite, miner.axon, synapse, timeout, on_event)


class ServingApiThread:
    """Runs the gateway on a private event loop in a daemon thread."""

    def __init__(self, app: FastAPI, host: str, port: int):
        config = uvicorn.Config(app, host=host, port=port, log_level='warning', loop='asyncio')
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self._run, name='serving-api', daemon=True)
        self.host = host
        self.port = port

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.server.serve())

    def start(self):
        self.thread.start()
        bt.logging.success(f'Serving API listening on {self.host}:{self.port} (OpenAI-compatible, /v1)')

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


def start_serving_api(
    state: ServingState,
    loadout: ServingLoadout,
    wallet: bt.Wallet,
    api_keys: Set[str],
    host: str,
    port: int,
    request_timeout: float,
    baseline_keys: Optional[Set[str]] = None,
) -> ServingApiThread:
    if not api_keys:
        raise ValueError('SERVING_API_KEYS is empty; refusing to start without API keys')
    app = build_app(
        state,
        loadout,
        api_keys | set(baseline_keys or ()),
        lambda: bt.Dendrite(wallet=wallet),
        request_timeout,
        baseline_keys=baseline_keys,
    )
    api = ServingApiThread(app, host, port)
    api.start()
    return api
