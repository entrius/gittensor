# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Server-sent-event plumbing for serving inference.

Every inference — audit or user traffic — travels validator -> miner as one
``InferenceSynapse`` streamed back as OpenAI ``chat.completion.chunk`` events,
so the miner can't tell the two apart and users get tokens as they decode.

- miner: ``OpenAICompatBackend.stream`` proxies the runtime's SSE bytes as-is;
  ``EchoBackend.stream`` and ``result_to_sse`` synthesise the same shape.
- validator: ``SSEParser`` splits bytes into events, ``StreamAssembler`` folds
  them into the completion / tokens / logprobs / usage the verifier and the
  non-streaming gateway path need, and ``consume_stream`` drives a dendrite
  ``call_stream`` to a filled synapse, optionally relaying each event.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Iterator, List, Optional

import bittensor as bt

from gittensor.serving.backends import GenerationResult
from gittensor.synapses import InferenceSynapse

SSE_DONE = b'data: [DONE]\n\n'
Event = Optional[dict]  # None marks [DONE]


def sse_event(payload: dict) -> bytes:
    return b'data: ' + json.dumps(payload, separators=(',', ':')).encode() + b'\n\n'


class SSEParser:
    """Splits a byte stream into ``data:`` events; ``[DONE]`` is yielded as None."""

    def __init__(self) -> None:
        self._buf = b''

    def feed(self, chunk: bytes) -> List[Event]:
        self._buf += chunk
        events: List[Event] = []
        while b'\n\n' in self._buf:
            raw, self._buf = self._buf.split(b'\n\n', 1)
            for line in raw.split(b'\n'):
                if not line.startswith(b'data:'):
                    continue
                data = line[5:].strip()
                if data == b'[DONE]':
                    events.append(None)
                    continue
                try:
                    events.append(json.loads(data))
                except ValueError:
                    continue
        return events


@dataclass
class StreamAssembler:
    """Folds chunk events into the response fields of ``InferenceSynapse``."""

    content: str = ''
    tokens: List[str] = field(default_factory=list)
    token_ids: List[int] = field(default_factory=list)
    token_bytes: List[List[int]] = field(default_factory=list)
    token_logprobs: List[float] = field(default_factory=list)
    model_id: Optional[str] = None
    finish_reason: Optional[str] = None
    usage: Dict[str, int] = field(default_factory=dict)
    ttft_ms: Optional[float] = None
    generation_ms: Optional[float] = None
    decode_tps: Optional[float] = None
    done: bool = False

    def feed(self, event: Event) -> None:
        if event is None:
            self.done = True
            return
        if event.get('model'):
            self.model_id = event['model']
        for choice in event.get('choices') or []:
            delta = choice.get('delta') or {}
            if delta.get('content'):
                self.content += delta['content']
            for entry in (choice.get('logprobs') or {}).get('content') or []:
                self.tokens.append(entry['token'])
                self.token_logprobs.append(float(entry['logprob']))
                if entry.get('token_id') is not None:
                    self.token_ids.append(int(entry['token_id']))
                if isinstance(entry.get('bytes'), list):
                    self.token_bytes.append([int(b) for b in entry['bytes']])
            if choice.get('finish_reason'):
                self.finish_reason = choice['finish_reason']
        usage = event.get('usage')
        if isinstance(usage, dict):
            self.usage = {k: v for k, v in usage.items() if isinstance(v, int)}
            for name in ('ttft_ms', 'generation_ms', 'decode_tps'):
                if usage.get(name) is not None:
                    setattr(self, name, float(usage[name]))

    def text(self) -> str:
        """The completion as the caller should receive it.

        A runtime decodes each token to text on its own, so a multibyte character split across two tokens arrives
        as U+FFFD in both deltas: a completion reading "between **<r>U+2308 m/2 <r>U+2309**" where the model wrote
        the ceiling brackets. The per-token bytes do not have that problem, so when every token reported them and
        they decode cleanly they are the completion; otherwise the deltas stand as sent.
        """
        if not self.token_bytes or len(self.token_bytes) != len(self.tokens):
            return self.content
        decoded = b''.join(bytes(b) for b in self.token_bytes).decode('utf-8', 'replace')
        return self.content if '\ufffd' in decoded else decoded

    def apply(self, synapse: InferenceSynapse) -> InferenceSynapse:
        """Write the assembled response onto ``synapse``; an unfinished stream leaves ``completion`` None (a miss)."""
        if not self.done:
            return synapse
        synapse.completion = self.text()
        synapse.served_model_id = self.model_id
        synapse.tokens = self.tokens or None
        synapse.token_ids = self.token_ids if self.token_ids and len(self.token_ids) == len(self.tokens) else None
        synapse.token_bytes = (
            self.token_bytes if self.token_bytes and len(self.token_bytes) == len(self.tokens) else None
        )
        synapse.token_logprobs = self.token_logprobs or None
        synapse.finish_reason = self.finish_reason
        synapse.usage = self.usage
        synapse.ttft_ms = self.ttft_ms
        synapse.generation_ms = self.generation_ms
        synapse.decode_tps = self.decode_tps
        return synapse


def result_to_sse(result: GenerationResult, request_id: str, created: int, logprobs: bool) -> Iterator[bytes]:
    """One completed generation as the chunk sequence a streaming runtime would have sent."""
    base = {'id': request_id, 'object': 'chat.completion.chunk', 'created': created, 'model': result.model_id}
    delta: dict = {'index': 0, 'delta': {'role': 'assistant', 'content': result.completion}, 'finish_reason': None}
    if logprobs and result.tokens and result.token_logprobs:
        ids = result.token_ids if result.token_ids and len(result.token_ids) == len(result.tokens) else None
        delta['logprobs'] = {
            'content': [
                {'token': t, 'logprob': lp, **({'token_id': ids[i]} if ids else {})}
                for i, (t, lp) in enumerate(zip(result.tokens, result.token_logprobs))
            ]
        }
    yield sse_event({**base, 'choices': [delta]})
    yield sse_event({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': result.finish_reason or 'stop'}]})
    usage: dict = dict(result.usage or {})
    for name in ('ttft_ms', 'generation_ms', 'decode_tps'):
        value = getattr(result, name, None)
        if value is not None:
            usage[name] = value
    yield sse_event({**base, 'choices': [], 'usage': usage})
    yield SSE_DONE


async def consume_stream(
    dendrite: bt.Dendrite,
    axon: bt.AxonInfo,
    synapse: InferenceSynapse,
    timeout: float,
    on_event: Optional[Callable[[Event], Awaitable[None]]] = None,
) -> InferenceSynapse:
    """Stream one inference from ``axon`` and return the filled synapse; relay each event to ``on_event`` if given."""
    parser, assembler = SSEParser(), StreamAssembler()
    final: Optional[InferenceSynapse] = None
    started = time.monotonic()
    observed_ttft_ms: Optional[float] = None
    async for chunk in dendrite.call_stream(target_axon=axon, synapse=synapse, timeout=timeout, deserialize=False):
        if isinstance(chunk, (bytes, bytearray)):
            for event in parser.feed(bytes(chunk)):
                if observed_ttft_ms is None:
                    observed_ttft_ms = (time.monotonic() - started) * 1000.0
                assembler.feed(event)
                if on_event is not None:
                    await on_event(event)
        else:
            final = chunk  # the dendrite yields the header-filled synapse last
    result = assembler.apply(final if final is not None else synapse)
    result.observed_ttft_ms = observed_ttft_ms
    result.observed_latency_ms = (time.monotonic() - started) * 1000.0  # the miner's stream, not the user's read
    return result
