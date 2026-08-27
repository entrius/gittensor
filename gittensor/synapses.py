# Entrius 2025
from typing import ClassVar, Dict, List, Optional

import bittensor as bt
from pydantic import Field


class PatBroadcastSynapse(bt.Synapse):
    """Miner-initiated push synapse to broadcast their GitHub PAT to validators.

    The miner sets github_access_token on the request. The validator validates the PAT
    (checks it works, extracts GitHub ID, runs a test query)
    and responds with accepted/rejection_reason.
    """

    # Miner request. repr=False keeps pydantic's default repr from emitting the
    # raw token; the explicit __repr__/__str__ below render a last-4-char tag so
    # masked log lines remain correlatable with rotated tokens.
    github_access_token: str = Field(repr=False)

    # Validator response
    accepted: Optional[bool] = None
    rejection_reason: Optional[str] = None

    def __repr__(self) -> str:
        token = self.github_access_token or ''
        masked = f'***{token[-4:]}' if len(token) >= 4 else '***'
        return (
            f'PatBroadcastSynapse(github_access_token={masked}, '
            f'accepted={self.accepted!r}, rejection_reason={self.rejection_reason!r})'
        )

    __str__ = __repr__


class InferenceSynapse(bt.StreamingSynapse):
    """Inference request for serving miners (sub-subnet B beta).

    Carries one OpenAI-style chat request from validator to miner. The same
    synapse is used for validator capacity probes and for gateway (user) traffic,
    so a miner cannot tell them apart. The miner answers with a stream of
    OpenAI ``chat.completion.chunk`` events (``gittensor/serving/stream.py``);
    the validator folds them into the response fields below. When ``logprobs``
    is set the chunks carry per-token logprobs of the greedy completion, which
    the validator checks against its reference (``gittensor/serving/audit.py``).
    """

    required_hash_fields: ClassVar[tuple[str, ...]] = ('messages', 'model_id', 'max_tokens', 'logprobs')

    # Request
    messages: List[Dict[str, str]]
    model_id: str
    max_tokens: int = 64
    logprobs: bool = False

    # Response
    completion: Optional[str] = None
    served_model_id: Optional[str] = None
    generation_ms: Optional[float] = None
    ttft_ms: Optional[float] = None
    decode_tps: Optional[float] = None
    tokens: Optional[List[str]] = None
    token_ids: Optional[List[int]] = None
    token_logprobs: Optional[List[float]] = None
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, int]] = None
    observed_ttft_ms: Optional[float] = None  # set by the validator: wall-clock to the first streamed event

    async def process_streaming_response(self, response):  # aiohttp ClientResponse
        async for chunk in response.content.iter_any():
            yield chunk

    def extract_response_json(self, response) -> dict:
        headers = {k.decode('utf-8'): v.decode('utf-8') for k, v in response.__dict__['_raw_headers']}

        def section(prefix: str) -> Dict[str, str]:
            return {k[len(prefix) :]: v for k, v in headers.items() if k.startswith(prefix)}

        return {
            'name': headers.get('name', ''),
            'timeout': float(headers.get('timeout', 0)),
            'total_size': int(headers.get('total_size', 0)),
            'header_size': int(headers.get('header_size', 0)),
            'dendrite': section('bt_header_dendrite_'),
            'axon': section('bt_header_axon_'),
            'messages': self.messages,
            'model_id': self.model_id,
            'max_tokens': self.max_tokens,
            'logprobs': self.logprobs,
        }


class PatCheckSynapse(bt.Synapse):
    """Probe for miners to check if a validator has their PAT stored and valid.

    No PAT is sent — the validator identifies the miner by their dendrite hotkey,
    looks up the stored PAT, and re-validates it (GitHub API check + test query).
    """

    # Validator response
    has_pat: Optional[bool] = None
    pat_valid: Optional[bool] = None
    rejection_reason: Optional[str] = None
