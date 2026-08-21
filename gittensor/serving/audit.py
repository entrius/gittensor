# The MIT License (MIT)
# Copyright © 2025 Entrius

"""References + tolerance verification for serving miners.

A *reference* answers one question for a release: "what does an honest copy
of this model+runtime produce for this prompt?" The verifier compares a
miner's greedy tokens/logprobs to the reference within a tolerance band. The
verifier is release-agnostic; only the reference knows which model it is.

Three references, picked per release by ``reference_for``:

- ``LiveReference`` — a conformant copy of the release reachable over HTTP
  (``reference_url``): the validator's own 5090, or a rented one kept warm
  (``reference_api_key`` for the bearer). Fresh prompt every audit; nothing to
  memorise; can also produce a reference for *any* prompt (mirrored traffic,
  disputes). This is the intended production reference.
- ``BankReference`` — a snapshot of a live reference (``audit_bank`` JSON built
  by ``scripts/build_serving_audit_bank.py``). Fallback for validators without
  a GPU or while the live runtime is down. Finite, so rotate it.
- ``EchoReference`` — derived on the fly for the deterministic echo backend so
  localnet needs neither a GPU nor a bank.

Verification is a tolerance band, not an exact match, because greedy decode
on a GPU is not bitwise deterministic across batches/kernels:

- ``prefix_agreement``: fraction of the reference token sequence the miner
  reproduced before first divergence. Substituted/quantized models fork the
  greedy path early on a meaningful share of prompts; honest nondeterminism
  forks rarely and late.
- ``mean_abs_logprob_diff``: mean |logprob_miner - logprob_reference| over the
  agreed prefix. A different model assigns visibly different probabilities to
  the same tokens even when it picks the same ones.

Both thresholds are PROVISIONAL and must be calibrated per release against
the runtime's own measured stability (``scripts/check_serving_runtime.py``)
before any emissions ride on them. Every audit requests ``logprobs=True``;
the gateway does the same for organic traffic so the flag is not a tell.
"""

import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Protocol, Sequence

from gittensor.constants import (
    SERVING_AUDIT_MAX_MEAN_ABS_LOGPROB_DIFF,
    SERVING_AUDIT_MIN_PREFIX_AGREEMENT,
)
from gittensor.serving.backends import Message, expected_completion
from gittensor.serving.loadout import WEIGHTS_DIR, ServingRelease
from gittensor.serving.probe import greedy, make_prompts


@dataclass
class AuditCase:
    messages: List[Message]
    max_tokens: int
    reference_tokens: List[str]
    reference_logprobs: List[float]
    reference_completion: str = ''


@dataclass
class AuditVerdict:
    passed: bool
    prefix_agreement: float
    mean_abs_logprob_diff: float
    reason: str

    def as_dict(self) -> dict:
        return {
            'passed': self.passed,
            'prefix_agreement': round(self.prefix_agreement, 4),
            'mean_abs_logprob_diff': round(self.mean_abs_logprob_diff, 4),
            'reason': self.reason,
        }


class Reference(Protocol):
    model_id: str

    def sample(self) -> AuditCase: ...

    def __len__(self) -> int: ...


class BankReference:
    """Snapshot reference: precomputed audit cases loaded from JSON."""

    def __init__(self, cases: Sequence[AuditCase], model_id: str, runtime_pin: Optional[str] = None):
        if not cases:
            raise ValueError('audit bank is empty')
        self.cases = list(cases)
        self.model_id = model_id
        self.runtime_pin = runtime_pin

    @classmethod
    def load(cls, path: Path) -> 'BankReference':
        with open(path) as f:
            raw = json.load(f)
        cases = [
            AuditCase(
                messages=c['messages'],
                max_tokens=int(c['max_tokens']),
                reference_tokens=list(c['reference_tokens']),
                reference_logprobs=[float(x) for x in c['reference_logprobs']],
                reference_completion=c.get('reference_completion', ''),
            )
            for c in raw['cases']
        ]
        return cls(cases, model_id=raw['model_id'], runtime_pin=raw.get('runtime_pin'))

    def sample(self) -> AuditCase:
        return secrets.choice(self.cases)

    def __len__(self) -> int:
        return len(self.cases)


class EchoReference:
    """On-the-fly audit cases for the deterministic echo backend (localnet)."""

    def __init__(self, release: ServingRelease):
        self.model_id = release.model_id
        self.max_tokens = release.max_tokens

    def sample(self) -> AuditCase:
        messages = [{'role': 'user', 'content': secrets.token_hex(16)}]
        ref = expected_completion(messages, self.max_tokens, self.model_id)
        return AuditCase(
            messages=messages,
            max_tokens=self.max_tokens,
            reference_tokens=ref.tokens or [],
            reference_logprobs=ref.token_logprobs or [],
            reference_completion=ref.completion,
        )

    def __len__(self) -> int:
        return 1


class LiveReference:
    """A conformant runtime the validator runs itself; produces references on demand."""

    def __init__(self, release: ServingRelease):
        if not release.reference_url:
            raise ValueError('live reference requires reference_url on the release')
        self.model_id = release.model_id
        self.base_url = release.reference_url
        self.max_tokens = release.max_tokens
        self.timeout = release.request_timeout
        self.api_key = release.reference_api_key

    def case_for(self, messages: List[Message], max_tokens: Optional[int] = None) -> AuditCase:
        """Reference for an arbitrary prompt (audit, mirrored traffic, dispute)."""
        ref = greedy(self.base_url, self.model_id, messages, max_tokens or self.max_tokens, self.timeout, self.api_key)
        return AuditCase(
            messages=messages,
            max_tokens=ref['max_tokens'],
            reference_tokens=ref['reference_tokens'],
            reference_logprobs=ref['reference_logprobs'],
            reference_completion=ref['reference_completion'],
        )

    def sample(self) -> AuditCase:
        messages = make_prompts(1, seed=secrets.randbits(64))[0]
        return self.case_for(messages)

    def __len__(self) -> int:
        return 1


def reference_for(release: ServingRelease) -> Reference:
    """Best available reference for a release: live runtime > bank snapshot > echo."""
    if release.reference_url and release.backend != 'echo':
        return LiveReference(release)
    if release.audit_bank is None:
        return EchoReference(release)
    bank = BankReference.load(WEIGHTS_DIR / release.audit_bank)
    if bank.model_id != release.model_id:
        raise ValueError(f'audit bank model {bank.model_id!r} does not match release model {release.model_id!r}')
    return bank


def verify_response(
    case: AuditCase,
    tokens: Optional[Sequence[str]],
    token_logprobs: Optional[Sequence[float]],
    min_prefix_agreement: float = SERVING_AUDIT_MIN_PREFIX_AGREEMENT,
    max_mean_abs_logprob_diff: float = SERVING_AUDIT_MAX_MEAN_ABS_LOGPROB_DIFF,
) -> AuditVerdict:
    """Compare a miner's greedy completion against the audit reference."""
    if not tokens or token_logprobs is None or len(tokens) != len(token_logprobs):
        return AuditVerdict(False, 0.0, float('inf'), 'missing or malformed logprobs')
    if not case.reference_tokens:
        return AuditVerdict(False, 0.0, float('inf'), 'empty reference')

    prefix = 0
    for mine, ref in zip(tokens, case.reference_tokens):
        if mine != ref:
            break
        prefix += 1
    agreement = prefix / len(case.reference_tokens)

    if prefix == 0:
        return AuditVerdict(False, 0.0, float('inf'), 'diverged at first token')

    diffs = [abs(float(a) - float(b)) for a, b in zip(token_logprobs[:prefix], case.reference_logprobs[:prefix])]
    mean_diff = sum(diffs) / len(diffs)

    if agreement < min_prefix_agreement:
        return AuditVerdict(False, agreement, mean_diff, f'prefix agreement {agreement:.2f} < {min_prefix_agreement}')
    if mean_diff > max_mean_abs_logprob_diff:
        return AuditVerdict(False, agreement, mean_diff, f'logprob drift {mean_diff:.3f} > {max_mean_abs_logprob_diff}')
    return AuditVerdict(True, agreement, mean_diff, 'ok')
