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

Verification is statistical, not an exact match, because greedy decode on a
GPU is not reproducible run to run (sparkinfer 1b8b962 forks the greedy path
on most prompts; contract §4). ``verify_response`` measures one audit:

- ``prefix_agreement``: fraction of the reference token sequence the miner
  reproduced before first divergence.
- ``mean_abs_logprob_diff``: mean |logprob_miner - logprob_reference| over the
  agreed prefix.
- ``positional_overlap``: fraction of positions whose token matches the
  reference, ignoring divergence. Same ranking as prefix agreement, less noisy.

No single audit decides anything — an honest miner's distribution overlaps a
cheater's. ``AuditWindow`` keeps the last ``SERVING_AUDIT_WINDOW`` overlaps per
(hotkey, release) and passes the miner when their mean clears a threshold
calibrated at a 1% honest false-positive rate for that many audits
(``SERVING_AUDIT_OVERLAP_THRESHOLDS``; derivation and raw data in
``docs/serving-experiments/2026-08-22-planted-cheater``). Missed or malformed
audits enter the window as 0. Every audit requests ``logprobs=True``; the
gateway does the same for organic traffic so the flag is not a tell.
"""

import json
import secrets
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Protocol, Sequence, Tuple

from gittensor.constants import (
    SERVING_AUDIT_MAX_MEAN_ABS_LOGPROB_DIFF,
    SERVING_AUDIT_MIN_PREFIX_AGREEMENT,
    SERVING_AUDIT_OVERLAP_THRESHOLDS,
    SERVING_AUDIT_WINDOW,
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
    """One audit's measurements. ``passed`` is the per-audit telemetry band; ``AuditWindow`` decides."""

    passed: bool
    prefix_agreement: float
    mean_abs_logprob_diff: float
    reason: str
    positional_overlap: float = 0.0

    def as_dict(self) -> dict:
        return {
            'passed': self.passed,
            'prefix_agreement': round(self.prefix_agreement, 4),
            'mean_abs_logprob_diff': round(self.mean_abs_logprob_diff, 4),
            'positional_overlap': round(self.positional_overlap, 4),
            'reason': self.reason,
        }


def overlap_threshold(n_audits: int, table: Sequence[Tuple[int, float]] = SERVING_AUDIT_OVERLAP_THRESHOLDS) -> float:
    """Pass bar for the mean overlap of ``n_audits`` audits: linear interpolation of the calibrated table."""
    if n_audits <= 0:
        return float('inf')
    rows = sorted(table)
    if n_audits <= rows[0][0]:
        return rows[0][1]
    for (k0, t0), (k1, t1) in zip(rows, rows[1:]):
        if n_audits <= k1:
            return t0 + (t1 - t0) * (n_audits - k0) / (k1 - k0)
    return rows[-1][1]


@dataclass
class WindowVerdict:
    passed: bool
    n_audits: int
    mean_overlap: float
    threshold: float

    def as_dict(self) -> dict:
        return {
            'passed': self.passed,
            'n_audits': self.n_audits,
            'mean_overlap': round(self.mean_overlap, 4),
            'threshold': round(self.threshold, 4),
        }


@dataclass
class AuditWindow:
    """Rolling per-(hotkey, release) record of positional overlaps; the thing that actually passes a miner.

    Keyed by hotkey so a UID that changes hands starts from an empty window.
    """

    size: int = SERVING_AUDIT_WINDOW
    thresholds: Sequence[Tuple[int, float]] = SERVING_AUDIT_OVERLAP_THRESHOLDS
    _overlaps: Dict[Tuple[str, str], Deque[float]] = field(default_factory=dict)

    def record(self, hotkey: str, model_id: str, overlap: float) -> None:
        key = (hotkey, model_id)
        if key not in self._overlaps:
            self._overlaps[key] = deque(maxlen=self.size)
        self._overlaps[key].append(max(0.0, min(1.0, float(overlap))))

    def verdict(self, hotkey: str, model_id: str) -> WindowVerdict:
        xs = self._overlaps.get((hotkey, model_id))
        if not xs:
            return WindowVerdict(False, 0, 0.0, float('inf'))
        mean = sum(xs) / len(xs)
        threshold = overlap_threshold(len(xs), self.thresholds)
        return WindowVerdict(mean >= threshold, len(xs), mean, threshold)


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
    """Measure one audit against the reference. ``passed`` is telemetry; feed ``positional_overlap`` to ``AuditWindow``."""
    if not tokens or token_logprobs is None or len(tokens) != len(token_logprobs):
        return AuditVerdict(False, 0.0, float('inf'), 'missing or malformed logprobs')
    if not case.reference_tokens:
        return AuditVerdict(False, 0.0, float('inf'), 'empty reference')

    n_ref = len(case.reference_tokens)
    prefix = 0
    for mine, ref in zip(tokens, case.reference_tokens):
        if mine != ref:
            break
        prefix += 1
    agreement = prefix / n_ref
    overlap = sum(1 for mine, ref in zip(tokens, case.reference_tokens) if mine == ref) / n_ref

    if prefix == 0:
        return AuditVerdict(False, 0.0, float('inf'), 'diverged at first token', overlap)

    diffs = [abs(float(a) - float(b)) for a, b in zip(token_logprobs[:prefix], case.reference_logprobs[:prefix])]
    mean_diff = sum(diffs) / len(diffs)

    if agreement < min_prefix_agreement:
        reason = f'prefix agreement {agreement:.2f} < {min_prefix_agreement}'
        return AuditVerdict(False, agreement, mean_diff, reason, overlap)
    if mean_diff > max_mean_abs_logprob_diff:
        reason = f'logprob drift {mean_diff:.3f} > {max_mean_abs_logprob_diff}'
        return AuditVerdict(False, agreement, mean_diff, reason, overlap)
    return AuditVerdict(True, agreement, mean_diff, 'ok', overlap)
