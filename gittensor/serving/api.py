# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Validator-hosted OpenAI-compatible inference API (sub-subnet B beta).

    POST /v1/chat/completions   Authorization: Bearer <key>

The validator serves this; each request is forwarded to the READY miner with
the fewest in-flight requests as the same ``InferenceSynapse`` the audit loop
uses, so miners cannot tell audits from real traffic. Keys are a static list
in ``SERVING_API_KEYS`` for now (a DB table once keys need minting/credits).
Off unless keys are set. Binds loopback by default; put it behind the host
reverse proxy / TLS like any other service (see docker-compose.vali.yml).

No queue: with no READY capacity it returns 429 so rejected demand stays
visible. Streaming is a later phase.

    OPENAI_BASE_URL=http://<host>:8790/v1 OPENAI_API_KEY=<key> ...
"""

import asyncio
import threading
import time
import uuid
from typing import Dict, List, Optional, Set

import bittensor as bt
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gittensor.constants import SERVING_MAX_TOKENS
from gittensor.serving.loadout import ServingLoadout
from gittensor.serving.state import ReadyMiner, RequestRecord, ServingState
from gittensor.synapses import InferenceSynapse

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
) -> FastAPI:
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
                {'id': loadout.model_id, 'object': 'model', 'owned_by': 'gittensor', 'runtime_pin': loadout.runtime_pin}
            ],
        }

    @app.get('/v1/serving/status')
    async def status(_: str = Depends(require_key)):
        snap = state.snapshot()
        snap['model_id'] = loadout.model_id
        snap['recent'] = [r.__dict__ for r in state.recent(50)]
        return snap

    @app.post('/v1/chat/completions')
    async def chat_completions(request: Request, _: str = Depends(require_key)):
        body = await request.json()
        messages = body.get('messages')
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail='messages must be a non-empty list')
        if body.get('stream'):
            raise HTTPException(status_code=400, detail='streaming is not supported yet')
        if body.get('n', 1) != 1:
            raise HTTPException(status_code=400, detail='n must be 1')
        max_tokens = int(body.get('max_tokens') or body.get('max_completion_tokens') or loadout.max_tokens)
        max_tokens = max(1, min(max_tokens, SERVING_MAX_TOKENS))
        want_logprobs = bool(body.get('logprobs', False))

        miner = state.acquire()
        if miner is None:
            raise HTTPException(status_code=429, detail='no READY serving capacity')

        request_id = f'chatcmpl-{uuid.uuid4().hex[:24]}'
        start = time.monotonic()
        try:
            result = await _dispatch(get_dendrite(), miner, messages, max_tokens, loadout, request_timeout)
        finally:
            state.release(miner.uid)
        latency_ms = (time.monotonic() - start) * 1000.0

        ok = result is not None and result.completion is not None
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
        if result is None or result.completion is None:
            return JSONResponse(
                status_code=502, content={'error': {'message': 'serving miner failed', 'uid': miner.uid}}
            )

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
            'created': int(time.time()),
            'model': result.served_model_id or loadout.model_id,
            'choices': [choice],
            'usage': result.usage or {},
            'gittensor': {
                'served_uid': miner.uid,
                'latency_ms': round(latency_ms, 1),
                'ttft_ms': result.ttft_ms,
                'decode_tps': result.decode_tps,
            },
        }

    return app


async def _dispatch(
    dendrite: bt.Dendrite,
    miner: ReadyMiner,
    messages: List[Dict[str, str]],
    max_tokens: int,
    loadout: ServingLoadout,
    timeout: float,
) -> Optional[InferenceSynapse]:
    # logprobs always requested so organic traffic is indistinguishable from audits on the wire.
    synapse = InferenceSynapse(messages=messages, model_id=loadout.model_id, max_tokens=max_tokens, logprobs=True)
    responses = await dendrite(axons=[miner.axon], synapse=synapse, deserialize=False, timeout=timeout)
    return responses[0] if responses else None


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
) -> ServingApiThread:
    if not api_keys:
        raise ValueError('SERVING_API_KEYS is empty; refusing to start without API keys')
    app = build_app(state, loadout, api_keys, lambda: bt.Dendrite(wallet=wallet), request_timeout)
    api = ServingApiThread(app, host, port)
    api.start()
    return api
