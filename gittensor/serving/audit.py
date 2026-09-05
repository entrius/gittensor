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
window (``serving.db`` next to ``state.npz``, ``gittensor/serving/store.py``) so a
restart is not a reset.

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
import re
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
    SERVING_QUARANTINE_ESCALATION,
    SERVING_QUARANTINE_MAX_STEPS,
    SERVING_QUARANTINE_S,
    SERVING_STRIKE_FORGET_S,
)
from gittensor.serving.backends import Message, expected_completion
from gittensor.serving.loadout import WEIGHTS_DIR, ServingRelease
from gittensor.serving.probe import compare, greedy, make_prompts, score

MAX_TOKEN_ID = 1 << 21  # no released vocabulary is this large; anything past it is not a token id


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
    # The reference's own count of the prompt's tokens (teacher-forced scoring reports it); None when it did not.
    # Speed credit allows for prefilling this many tokens, so the number must never come from the miner.
    prompt_tokens: Optional[int] = None

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
    strikes: int = 0  # lifetime wrong answers on this (hotkey, release)

    def as_dict(self) -> dict:
        return {
            'passed': self.passed,
            'n_audits': self.n_audits,
            'mean': round(self.mean, 4),
            'threshold': round(self.threshold, 4),
            'quarantined_until': round(self.quarantined_until, 1),
            'strikes': self.strikes,
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
    _quarantine: Dict[Tuple[str, str], float] = field(default_factory=dict)  # (hotkey, release) -> until ts
    _strikes: Dict[Tuple[str, str], int] = field(default_factory=dict)  # (hotkey, release) -> strikes on the ladder
    _last_strike: Dict[Tuple[str, str], float] = field(default_factory=dict)  # (hotkey, release) -> ts of the last

    def record(self, hotkey: str, release_id: str, value: float) -> None:
        key = (hotkey, release_id)
        if key not in self._values:
            self._values[key] = deque(maxlen=self.size)
        self._values[key].append(max(0.0, min(1.0, float(value))))

    def strike(self, hotkey: str, release_id: str, now: Optional[float] = None) -> float:
        """A wrong answer: wipe the window and quarantine the (hotkey, release) until the returned timestamp. Each
        strike within ``SERVING_STRIKE_FORGET_S`` of the last quarantines ``SERVING_QUARANTINE_ESCALATION`` times
        longer, up to ``SERVING_QUARANTINE_MAX_STEPS`` steps: a strike costs a fresh hotkey an hour, a repeat offender
        most of a day. A strike after a clean week starts the ladder over."""
        key = (hotkey, release_id)
        now = now if now is not None else time.time()
        self._values.pop(key, None)
        self._strikes[key] = self.strikes(hotkey, release_id, now) + 1
        self._last_strike[key] = now
        step = min(self._strikes[key] - 1, SERVING_QUARANTINE_MAX_STEPS)
        until = now + self.quarantine_s * SERVING_QUARANTINE_ESCALATION**step
        self._quarantine[key] = until
        return until

    def strikes(self, hotkey: str, release_id: str, now: Optional[float] = None) -> int:
        """Strikes still on the ladder: none once the last one is ``SERVING_STRIKE_FORGET_S`` old."""
        key = (hotkey, release_id)
        n = self._strikes.get(key, 0)
        if n and (now if now is not None else time.time()) - self._last_strike.get(key, 0.0) > SERVING_STRIKE_FORGET_S:
            self._strikes.pop(key, None)
            self._last_strike.pop(key, None)
            return 0
        return n

    def quarantined_until(self, hotkey: str, release_id: str, now: Optional[float] = None) -> float:
        until = self._quarantine.get((hotkey, release_id), 0.0)
        return until if until > (now if now is not None else time.time()) else 0.0

    def to_dict(self) -> dict:
        return {
            'size': self.size,
            'values': [[hk, rid, list(xs)] for (hk, rid), xs in self._values.items()],
            'quarantine': [[hk, rid, until] for (hk, rid), until in self._quarantine.items()],
            'strikes': [[hk, rid, n, self._last_strike.get((hk, rid), 0.0)] for (hk, rid), n in self._strikes.items()],
        }

    @classmethod
    def from_dict(cls, raw: dict, **kwargs) -> 'AuditWindow':
        window = cls(**kwargs)
        for hk, rid, xs in raw.get('values', []):
            for x in xs[-window.size :]:
                window.record(str(hk), str(rid), float(x))
        for hk, rid, until in raw.get('quarantine', []):
            window._quarantine[(str(hk), str(rid))] = float(until)
        for hk, rid, n, *last in raw.get('strikes', []):
            window._strikes[(str(hk), str(rid))] = int(n)
            if last:
                window._last_strike[(str(hk), str(rid))] = float(last[0])
        return window

    def verdict(self, hotkey: str, release_id: str, now: Optional[float] = None) -> WindowVerdict:
        until = self.quarantined_until(hotkey, release_id, now)
        strikes = self.strikes(hotkey, release_id, now)
        xs = self._values.get((hotkey, release_id))
        if not xs:
            return WindowVerdict(False, 0, 0.0, float('inf'), until, strikes)
        mean = sum(xs) / len(xs)
        threshold = window_threshold(len(xs), self.thresholds)
        return WindowVerdict(mean >= threshold and until == 0.0, len(xs), mean, threshold, until, strikes)


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
        """Teacher-forced scoring for the echo backend: the argmax is the expected token at every position. The echo
        model never stops early, so a forced position past the text (an end-of-turn token) scores as a wrong token."""
        tokens = completion.split(' ') if completion else []
        n = max(len(tokens), len(token_ids) if token_ids else 0)
        ref = expected_completion(messages, max(1, n), self.model_id)
        argmax = list(ref.tokens or [])[:n]
        ref_lp = list(ref.token_logprobs or [])
        mine = tokens + ['<|im_end|>'] * (n - len(tokens))
        logprobs = [ref_lp[i] if i < len(ref_lp) and mine[i] == argmax[i] else -20.0 for i in range(n)]
        return {'tokens': mine, 'logprobs': logprobs, 'argmax': argmax, 'usage': {}}

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
    aligned: bool = False,
) -> AuditVerdict:
    """Measure one audit against the reference; ``passed`` requires every band to hold. With ``aligned`` (the
    reference scored exactly the miner's positions) a divergence at the first token is a wrong answer like any
    other, not a miss a miner can choose over a strike."""
    if not tokens or token_logprobs is None or len(tokens) != len(token_logprobs):
        return AuditVerdict(False, 0.0, float('inf'), 'missing or malformed logprobs')
    if not case.reference_tokens or len(case.reference_logprobs) != len(case.reference_tokens):
        return AuditVerdict(False, 0.0, float('inf'), 'empty or malformed reference')

    prefix, agreement, overlap, diffs = compare(tokens, token_logprobs, case.reference_tokens, case.reference_logprobs)
    if prefix == 0:
        return AuditVerdict(False, 0.0, float('inf'), 'diverged at first token', overlap, hard=aligned)

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
    release: Optional[ServingRelease] = None,
    max_tokens: Optional[int] = None,
) -> AuditVerdict:
    """Verify a served (greedy) completion by teacher forcing it under the reference.

    The reference's argmax at each position is what an honest copy would have generated, so the miner's tokens
    must match it and the miner's logprobs must match the reference's logprob of that same token. When the miner
    reported ``token_ids`` the reference forces exactly that sequence; otherwise it re-tokenizes the text, and a
    text that re-tokenizes to a different length (a greedy decode is not always the canonical tokenization of
    its own output) is not comparable position by position and counts as a soft miss. The bands (the release's,
    else the constants) failing on aligned lengths is a wrong answer (``hard``). A reference that echoes the forced tokens' bytes lets the ids
    be bound to the text the user actually received.
    """
    if completion is None or not tokens or token_logprobs is None or len(tokens) != len(token_logprobs):
        return AuditVerdict(False, 0.0, float('inf'), 'missing or malformed logprobs')
    mine, mine_lp = list(tokens), list(token_logprobs)
    if max_tokens is not None and len(mine) > max_tokens + 1:  # + the end-of-turn token a runtime lists
        return AuditVerdict(False, 0.0, float('inf'), f'{len(mine)} tokens for a {max_tokens}-token request')
    # Everything below is the miner's data; a value the reference could not even be asked about is the miner's
    # miss, never a reference fault (an exception here must not become a neutral verdict).
    mine_ids = list(token_ids) if token_ids and len(token_ids) == len(mine) else None
    if mine_ids is not None and not all(isinstance(i, int) and 0 <= i < MAX_TOKEN_ID for i in mine_ids):
        return AuditVerdict(False, 0.0, float('inf'), 'malformed token ids')
    mine_bytes: Optional[List[bytes]] = None
    if token_bytes and len(token_bytes) == len(mine):
        try:
            mine_bytes = [bytes(b) for b in token_bytes]
        except (TypeError, ValueError):
            return AuditVerdict(False, 0.0, float('inf'), 'malformed token bytes')
    if release is not None and release.backend != 'echo' and mine_ids is None:
        # A real runtime reports ids (contract R8); without them the text would be re-tokenized, and a miner
        # could pick a text whose fresh tokenization never aligns \u2014 a miss it can repeat forever, never a strike.
        return AuditVerdict(False, 0.0, float('inf'), 'no token ids')
    ended = bool(mine) and mine[-1] in end_of_turn
    if ended and mine_ids is None:  # the text path cannot force a position past the text; drop the end-of-turn
        mine, mine_lp, mine_bytes, ended = mine[:-1], mine_lp[:-1], mine_bytes[:-1] if mine_bytes else None, False
    content_n = len(mine) - 1 if ended else len(mine)
    if content_n <= 0:
        return AuditVerdict(False, 0.0, float('inf'), 'empty completion')
    appended = False
    if max_tokens is not None and content_n < max_tokens and not ended:
        # Stopped short of the budget with no end-of-turn token in the transcript (sparkinfer 7498736 lists only
        # the content tokens on a natural stop). The validator appends the release's end-of-turn id itself and
        # judges that position on the reference's argmax alone: the model must have chosen to stop here.
        eos_id = release.end_of_turn_token_id if release is not None else None
        if mine_ids is None or eos_id is None:
            return AuditVerdict(
                False, 0.0, float('inf'), f'stopped at {content_n} of {max_tokens} tokens without end-of-turn'
            )
        mine_ids, appended = mine_ids + [eos_id], True
    # An end-of-turn position — the miner's or the appended one — is forced like any other: the reference's argmax
    # there must be the end-of-turn, which is what makes an early stop the model's own decision and not the miner's.
    ref = reference.score_served(messages, completion, mine_ids)
    argmax, ref_lp = list(ref.get('argmax') or []), list(ref.get('logprobs') or [])
    reported = (ref.get('usage') or {}).get('prompt_tokens')
    prompt_tokens = int(reported) if isinstance(reported, int) and reported > 0 else None
    if appended:
        if len(argmax) == len(mine) + 1 and argmax[-1] not in end_of_turn:
            return AuditVerdict(False, 0.0, float('inf'), 'stopped early: the model would have continued', hard=True)
        argmax, ref_lp = argmax[: len(mine)], ref_lp[: len(mine)]
    if len(argmax) != len(mine) or len(ref_lp) != len(mine):
        return AuditVerdict(False, 0.0, float('inf'), f'tokenization mismatch ({len(mine)} vs {len(argmax)})')
    if mine_ids and ref.get('bytes'):
        # Bind the forced ids to what the user received: exact on bytes when the miner reported them, and the
        # decoded text must be the completion \u2014 a multibyte character split across streamed tokens shows up as
        # replacement characters in the stream's text, so each run of them stands for one or more non-ASCII
        # characters and nothing else.
        ref_bytes = b''.join(ref['bytes'][:content_n])
        if mine_bytes is not None and b''.join(mine_bytes[:content_n]) != ref_bytes:
            return AuditVerdict(False, 0.0, float('inf'), 'token ids do not spell the streamed bytes')
        if not spells(ref_bytes, completion):
            return AuditVerdict(False, 0.0, float('inf'), 'token ids do not spell the completion')
    case = AuditCase(messages=list(messages), max_tokens=len(mine), reference_tokens=argmax, reference_logprobs=ref_lp)
    if release is None:
        verdict = verify_response(case, mine, mine_lp, aligned=True)
    else:
        verdict = verify_response(
            case,
            mine,
            mine_lp,
            release.min_prefix_agreement,
            release.max_mean_abs_logprob_diff,
            release.max_abs_logprob_diff,
            aligned=True,
        )
    verdict.prompt_tokens = prompt_tokens
    return verdict


def spells(ref_bytes: bytes, completion: str) -> bool:
    """Does the decoded reference text read as ``completion``? Every maximal run of replacement characters in the
    completion stands for zero or more non-ASCII characters; nothing else may differ, so ASCII cannot be added,
    dropped or changed behind a valid transcript.

    Zero, not one: a runtime that splits a multibyte character across two chunks decodes the first chunk on its
    own and streams U+FFFD for it, then streams the whole character once its bytes are in - so the run is beside
    the character it stands for rather than in place of it, and the reference has nothing there to consume.
    Measured on the blessed pin in soak 7: the caller was streamed "**<r>U+2308 m/2 <r>U+2309**" where the
    reference reads "**U+2308 m/2 U+2309**", and requiring one character per run failed honest miners."""
    text = ref_bytes.decode('utf-8', 'replace')
    if '\ufffd' not in completion:
        return text == completion
    pattern = ''.join(
        f'[^\\x00-\\x7f]{{0,{len(run)}}}' if run.startswith('\ufffd') else re.escape(run)
        for run in re.findall('\ufffd+|[^\ufffd]+', completion)
    )
    return re.fullmatch(pattern, text, re.DOTALL) is not None
