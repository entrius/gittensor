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

``verify_response`` measures one audit against the reference's greedy output:

- ``prefix_agreement``: fraction of the reference token sequence the miner
  reproduced before first divergence.
- ``mean_abs_logprob_diff`` / ``max_abs_logprob_diff``: |logprob_miner -
  logprob_reference| over the agreed prefix.
- ``positional_overlap``: fraction of positions whose token matches the
  reference, ignoring divergence (telemetry).

The blessed runtime is bit-reproducible (sparkinfer 7498736 with
``SPARKINFER_DETERMINISTIC=1``), so an honest miner reproduces the reference
exactly and ``passed`` is decisive per audit: all tokens match and logprobs
agree to float noise (``SERVING_AUDIT_*`` bands in constants.py, calibrated in
the 2026-08-24 measurements, kept in internal notes). ``AuditWindow``
keeps the last ``SERVING_AUDIT_WINDOW`` outcomes per (hotkey, release) and
publishes the miner while their mean clears ``SERVING_AUDIT_WINDOW_THRESHOLDS``
— it absorbs transient misses; it is not there to average out model noise (that
was the 1b8b962 design, 2026-08-22 notes).
Missed or malformed responses enter the window as 0. The validator persists the
window (``serving_audits.json`` next to ``state.npz``) so a restart is not a
reset.

There are no synthetic audit prompts: every request served through the gateway
*is* the audit. ``verify_served`` teacher-forces the miner's completion under
the reference (``Reference.score_served``, contract R8) and compares the miner's
tokens/logprobs to the reference's argmax/logprobs position by position — one
prefill pass, no generation, and nothing on the wire that a miner could tell
apart from unaudited traffic. A verdict with ``hard`` set (tokens or logprobs
outside the bands with aligned lengths) is a wrong answer, not a miss:
``AuditWindow.strike`` wipes the window and quarantines the hotkey.
"""

import json
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Protocol, Sequence, Tuple

from gittensor.constants import (
    SERVING_AUDIT_MAX_ABS_LOGPROB_DIFF,
    SERVING_AUDIT_MAX_MEAN_ABS_LOGPROB_DIFF,
    SERVING_AUDIT_MIN_PREFIX_AGREEMENT,
    SERVING_AUDIT_WINDOW,
    SERVING_AUDIT_WINDOW_THRESHOLDS,
    SERVING_QUARANTINE_S,
)
from gittensor.serving.backends import Message, expected_completion
from gittensor.serving.loadout import WEIGHTS_DIR, ServingRelease
from gittensor.serving.probe import compare, greedy, make_prompts, score


@dataclass
class AuditCase:
    messages: List[Message]
    max_tokens: int
    reference_tokens: List[str]
    reference_logprobs: List[float]
    reference_completion: str = ''


@dataclass
class AuditVerdict:
    """One audit's measurements; ``passed`` is the verdict that enters the window."""

    passed: bool
    prefix_agreement: float
    mean_abs_logprob_diff: float
    reason: str
    positional_overlap: float = 0.0
    max_abs_logprob_diff: float = float('inf')
    hard: bool = False  # a wrong answer (bands failed with aligned lengths), not a miss

    @property
    def value(self) -> float:
        """What the window records: 1 for a pass, 0 otherwise."""
        return 1.0 if self.passed else 0.0

    def as_dict(self) -> dict:
        return {
            'passed': self.passed,
            'prefix_agreement': round(self.prefix_agreement, 4),
            'mean_abs_logprob_diff': round(self.mean_abs_logprob_diff, 4),
            'positional_overlap': round(self.positional_overlap, 4),
            'max_abs_logprob_diff': round(self.max_abs_logprob_diff, 4),
            'reason': self.reason,
            'hard': self.hard,
        }


def window_threshold(n_audits: int, table: Sequence[Tuple[int, float]] = SERVING_AUDIT_WINDOW_THRESHOLDS) -> float:
    """Pass bar for the window mean after ``n_audits`` audits: linear interpolation of the table, flat beyond it."""
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
    mean: float
    threshold: float
    quarantined_until: float = 0.0

    def as_dict(self) -> dict:
        return {
            'passed': self.passed,
            'n_audits': self.n_audits,
            'mean': round(self.mean, 4),
            'threshold': round(self.threshold, 4),
            'quarantined_until': round(self.quarantined_until, 1),
        }


@dataclass
class AuditWindow:
    """Rolling per-(hotkey, release) record of audit outcomes in [0, 1]; publishes a miner while the mean holds.

    Keyed by hotkey so a UID that changes hands starts from an empty window.
    """

    size: int = SERVING_AUDIT_WINDOW
    thresholds: Sequence[Tuple[int, float]] = SERVING_AUDIT_WINDOW_THRESHOLDS
    quarantine_s: float = SERVING_QUARANTINE_S
    _values: Dict[Tuple[str, str], Deque[float]] = field(default_factory=dict)
    _quarantine: Dict[Tuple[str, str], float] = field(default_factory=dict)  # (hotkey, model) -> until ts

    def record(self, hotkey: str, model_id: str, value: float) -> None:
        key = (hotkey, model_id)
        if key not in self._values:
            self._values[key] = deque(maxlen=self.size)
        self._values[key].append(max(0.0, min(1.0, float(value))))

    def strike(self, hotkey: str, model_id: str, now: Optional[float] = None) -> float:
        """A wrong answer: wipe the window and quarantine the (hotkey, release) until the returned timestamp."""
        key = (hotkey, model_id)
        self._values.pop(key, None)
        until = (now if now is not None else time.time()) + self.quarantine_s
        self._quarantine[key] = until
        return until

    def quarantined_until(self, hotkey: str, model_id: str, now: Optional[float] = None) -> float:
        until = self._quarantine.get((hotkey, model_id), 0.0)
        return until if until > (now if now is not None else time.time()) else 0.0

    def to_dict(self) -> dict:
        return {
            'size': self.size,
            'values': [[hk, mid, list(xs)] for (hk, mid), xs in self._values.items()],
            'quarantine': [[hk, mid, until] for (hk, mid), until in self._quarantine.items()],
        }

    @classmethod
    def from_dict(cls, raw: dict, **kwargs) -> 'AuditWindow':
        window = cls(**kwargs)
        for hk, mid, xs in raw.get('values', []):
            for x in xs[-window.size :]:
                window.record(str(hk), str(mid), float(x))
        for hk, mid, until in raw.get('quarantine', []):
            window._quarantine[(str(hk), str(mid))] = float(until)
        return window

    def verdict(self, hotkey: str, model_id: str, now: Optional[float] = None) -> WindowVerdict:
        until = self.quarantined_until(hotkey, model_id, now)
        xs = self._values.get((hotkey, model_id))
        if not xs:
            return WindowVerdict(False, 0, 0.0, float('inf'), until)
        mean = sum(xs) / len(xs)
        threshold = window_threshold(len(xs), self.thresholds)
        return WindowVerdict(mean >= threshold and until == 0.0, len(xs), mean, threshold, until)


class Reference(Protocol):
    def score_served(
        self, messages: List[Message], completion: str, token_ids: Optional[Sequence[int]] = None
    ) -> dict: ...

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

    def score_served(self, messages: List[Message], completion: str, token_ids: Optional[Sequence[int]] = None) -> dict:
        raise NotImplementedError('a bank reference cannot verify served traffic; run a live reference')

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

    def score_served(self, messages: List[Message], completion: str, token_ids: Optional[Sequence[int]] = None) -> dict:
        """Teacher-forced scoring for the echo backend: the argmax is the expected token at every position."""
        tokens = completion.split(' ') if completion else []
        ref = expected_completion(messages, max(1, len(tokens)), self.model_id)
        argmax = list(ref.tokens or [])[: len(tokens)]
        ref_lp = list(ref.token_logprobs or [])
        logprobs = [ref_lp[i] if i < len(ref_lp) and tokens[i] == argmax[i] else -20.0 for i in range(len(tokens))]
        return {'tokens': tokens, 'logprobs': logprobs, 'argmax': argmax, 'usage': {}}

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

    def score(self, messages: List[Message], completion: str, token_ids: Optional[Sequence[int]] = None) -> dict:
        """Teacher-forced logprobs of ``completion`` under the reference (R8): verifies text the reference
        did not generate. With ``token_ids`` the reference forces the miner's own tokens rather than
        re-tokenizing the text. A trailing end-of-turn token in the miner's token list is not part of the
        text; ``verify_served`` strips it before comparing lengths."""
        return score(
            self.base_url,
            self.model_id,
            messages,
            completion,
            self.timeout,
            self.api_key,
            completion_token_ids=token_ids,
        )

    def score_served(self, messages: List[Message], completion: str, token_ids: Optional[Sequence[int]] = None) -> dict:
        return self.score(messages, completion, token_ids)

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
    max_abs_logprob_diff: float = SERVING_AUDIT_MAX_ABS_LOGPROB_DIFF,
) -> AuditVerdict:
    """Measure one audit against the reference; ``passed`` requires every band to hold."""
    if not tokens or token_logprobs is None or len(tokens) != len(token_logprobs):
        return AuditVerdict(False, 0.0, float('inf'), 'missing or malformed logprobs')
    if not case.reference_tokens or len(case.reference_logprobs) != len(case.reference_tokens):
        return AuditVerdict(False, 0.0, float('inf'), 'empty or malformed reference')

    prefix, agreement, overlap, diffs = compare(tokens, token_logprobs, case.reference_tokens, case.reference_logprobs)
    if prefix == 0:
        return AuditVerdict(False, 0.0, float('inf'), 'diverged at first token', overlap)

    mean_diff = sum(diffs) / len(diffs)
    max_diff = max(diffs)

    if agreement < min_prefix_agreement:
        reason = f'prefix agreement {agreement:.2f} < {min_prefix_agreement}'
        return AuditVerdict(False, agreement, mean_diff, reason, overlap, max_diff, hard=True)
    if mean_diff > max_mean_abs_logprob_diff:
        reason = f'logprob drift {mean_diff:.4f} > {max_mean_abs_logprob_diff}'
        return AuditVerdict(False, agreement, mean_diff, reason, overlap, max_diff, hard=True)
    if max_diff > max_abs_logprob_diff:
        reason = f'logprob outlier {max_diff:.4f} > {max_abs_logprob_diff}'
        return AuditVerdict(False, agreement, mean_diff, reason, overlap, max_diff, hard=True)
    return AuditVerdict(True, agreement, mean_diff, 'ok', overlap, max_diff)


def verify_served(
    reference: Reference,
    messages: List[Message],
    completion: Optional[str],
    tokens: Optional[Sequence[str]],
    token_logprobs: Optional[Sequence[float]],
    token_ids: Optional[Sequence[int]] = None,
    end_of_turn: Sequence[str] = ('<|im_end|>', '<|endoftext|>', '</s>'),
    token_bytes: Optional[Sequence[Sequence[int]]] = None,
) -> AuditVerdict:
    """Verify a served (greedy) completion by teacher forcing it under the reference.

    The reference's argmax at each position is what an honest copy would have generated, so the miner's tokens
    must match it and the miner's logprobs must match the reference's logprob of that same token. When the miner
    reported ``token_ids`` the reference forces exactly that sequence; otherwise it re-tokenizes the text, and a
    text that re-tokenizes to a different length (a greedy decode is not always the canonical tokenization of
    its own output) is not comparable position by position and counts as a soft miss. The bands failing on
    aligned lengths is a wrong answer (``hard``). A reference that echoes the forced tokens' bytes lets the ids
    be bound to the text the user actually received.
    """
    if completion is None or not tokens or token_logprobs is None or len(tokens) != len(token_logprobs):
        return AuditVerdict(False, 0.0, float('inf'), 'missing or malformed logprobs')
    mine, mine_lp = list(tokens), list(token_logprobs)
    mine_ids = list(token_ids) if token_ids and len(token_ids) == len(mine) else None
    mine_bytes = [bytes(b) for b in token_bytes] if token_bytes and len(token_bytes) == len(mine) else None
    if mine and mine[-1] in end_of_turn:
        mine, mine_lp = mine[:-1], mine_lp[:-1]
        mine_ids = mine_ids[:-1] if mine_ids else None
        mine_bytes = mine_bytes[:-1] if mine_bytes else None
    if not mine:
        return AuditVerdict(False, 0.0, float('inf'), 'empty completion')
    ref = reference.score_served(messages, completion, mine_ids)
    argmax, ref_lp = ref.get('argmax') or [], ref.get('logprobs') or []
    if len(argmax) != len(mine) or len(ref_lp) != len(mine):
        return AuditVerdict(False, 0.0, float('inf'), f'tokenization mismatch ({len(mine)} vs {len(argmax)})')
    if mine_ids and ref.get('bytes'):
        # Bind the forced ids to what the user received. Exact on bytes when the miner reported them; the decoded
        # text is only compared when it decodes cleanly (a multibyte character split across streamed tokens shows
        # up as replacement characters in the stream's text and is not the miner's doing).
        ref_bytes = b''.join(ref['bytes'])
        if mine_bytes is not None:
            if b''.join(mine_bytes) != ref_bytes:
                return AuditVerdict(False, 0.0, float('inf'), 'token ids do not spell the streamed bytes')
        else:
            text = ref_bytes.decode('utf-8', 'replace')
            if '\ufffd' not in text and '\ufffd' not in completion and text != completion:
                return AuditVerdict(False, 0.0, float('inf'), 'token ids do not spell the completion')
    case = AuditCase(messages=list(messages), max_tokens=len(mine), reference_tokens=argmax, reference_logprobs=ref_lp)
    return verify_response(case, mine, mine_lp)
