# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Audit bank + tolerance verification for serving miners (sub-subnet B beta).

The validator cannot re-run the model itself at launch (no validator GPU), so
correctness is checked against a *precomputed audit bank*: a JSON file of
chat prompts with the reference greedy completion and per-token logprobs
produced by the pinned runtime on the pinned model. The bank is built by
``scripts/build_serving_audit_bank.py`` against a trusted sparkinfer_server
and shipped on the same rail as the loadout.

Verification is a tolerance band, not an exact match, because greedy decode
on a GPU is not bitwise deterministic across batches/kernels:

- ``prefix_agreement``: fraction of the reference token sequence the miner
  reproduced before first divergence. Substituted/quantized models fork the
  greedy path early on a meaningful share of prompts; honest nondeterminism
  forks rarely and late.
- ``mean_abs_logprob_diff``: mean |logprob_miner - logprob_reference| over the
  agreed prefix. A different model assigns visibly different probabilities to
  the same tokens even when it picks the same ones.

Both thresholds are PROVISIONAL and must be calibrated against a real 5090
(honest runtime vs. planted quant vs. planted proxy) before any emissions ride
on them.

For the echo backend the bank is derived on the fly (``EchoAuditBank``), so
localnet needs no bank file. Every audit requests ``logprobs=True``; the
gateway does the same for organic traffic so the flag is not a tell.
"""

import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from gittensor.constants import (
    SERVING_AUDIT_MAX_MEAN_ABS_LOGPROB_DIFF,
    SERVING_AUDIT_MIN_PREFIX_AGREEMENT,
)
from gittensor.serving.backends import Message, expected_completion
from gittensor.serving.loadout import WEIGHTS_DIR, ServingLoadout


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


class AuditBank:
    """Fixed bank of precomputed audit cases loaded from JSON."""

    def __init__(self, cases: Sequence[AuditCase], model_id: str, runtime_pin: Optional[str] = None):
        if not cases:
            raise ValueError('audit bank is empty')
        self.cases = list(cases)
        self.model_id = model_id
        self.runtime_pin = runtime_pin

    @classmethod
    def load(cls, path: Path) -> 'AuditBank':
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


class EchoAuditBank:
    """On-the-fly audit cases for the deterministic echo backend (localnet)."""

    def __init__(self, loadout: ServingLoadout):
        self.model_id = loadout.model_id
        self.max_tokens = loadout.max_tokens

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


def load_audit_bank(loadout: ServingLoadout):
    if loadout.audit_bank is None:
        return EchoAuditBank(loadout)
    bank = AuditBank.load(WEIGHTS_DIR / loadout.audit_bank)
    if bank.model_id != loadout.model_id:
        raise ValueError(f'audit bank model {bank.model_id!r} does not match loadout model {loadout.model_id!r}')
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
