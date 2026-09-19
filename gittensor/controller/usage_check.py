# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The lease accounting check: a leased card serves the gateway's traffic only (Kimbo 9/18). Pure, no I/O.

A LEASED card is ours alone, and the pool pays for it by the card-hour; every request it serves comes through our
gateway. So the completion tokens the runtime says it made (its own ``/metrics`` counters) are at most what the
gateway sent it. The watch reads both on every heartbeat visit: the gateway's ``/healthz`` once before and once after
the runtime's counters, and this module judges each leased instance from those three reads.

**Upper bounds only, every one in the miner's favour.** A number we cannot know exactly is replaced by the most it
could be:

* a request whose completion tokens the gateway did not learn (no ``usage``, a client that left mid-stream, an upstream
  error after bytes were sent) counts as its own ``max_tokens``, or the runtime's output ceiling when it named none
  (``unaccounted_allowance_tokens``);
* a request still in flight at the second gateway read counts as the ceiling;
* the gateway's totals run from the first read of the baseline to the second read of the sample, a window that
  covers the runtime's on both sides, so a request that finishes between two reads is counted, never lost.

From the first sample after the card is LEASED (the baseline; the start's canary is before it) the check works in
deltas: ``surplus = Δruntime − Δgateway − Δunaccounted allowance − in flight now × ceiling``. Over
``max(EXTERNAL_USE_MIN_TOKENS, EXTERNAL_USE_MIN_FRACTION × Δruntime)`` is a strike; ``EXTERNAL_USE_STRIKES`` in a row
(one per visit) are a detection, and anything else clears the count. A runtime counter that went down (the runtime
restarted) or a gateway that restarted starts a new baseline and clears the strikes. With no gateway to read, no
counters (a runtime not in ``RUNTIME_COUNTERS``, a ``/metrics`` that does not answer or lacks the series) or a gateway
without the totals: no judgement, logged once, never a strike.

Only completion tokens are judged: the keeper's probes and the health probe are requests too, and make none.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from gittensor.controller.checks import config as cfg
from gittensor.controller.manifest import Manifest

BASELINE, REBASELINE, CLEAR, STRIKE, DETECTION, NO_JUDGEMENT = (
    'baseline',
    'rebaseline',
    'clear',
    'strike',
    'detection',
    'no_judgement',
)
THROUGHPUT_LOW = 'throughput_low'

_SERIES = re.compile(r'^([A-Za-z_:][A-Za-z0-9_:]*)(\{(.*)\})?\s+(\S+)(\s+\S+)?$')
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def output_ceiling(manifest: Manifest | None) -> int:
    """The most completion tokens one request to an instance of ``manifest`` can make when it names no ``max_tokens``:
    ``RUNTIME_OUTPUT_CEILING_TOKENS``, or more when the manifest names a higher limit (``profile.max_output_tokens``, or
    the runtime's ``SPARKINFER_MAX_OUTPUT_TOKENS``)."""
    ceiling = cfg.RUNTIME_OUTPUT_CEILING_TOKENS
    if manifest is None:
        return ceiling
    named = [manifest.profile.get('max_output_tokens'), manifest.run.env.get('SPARKINFER_MAX_OUTPUT_TOKENS')]
    for value in named:
        try:
            ceiling = max(ceiling, int(str(value)))
        except (TypeError, ValueError):
            continue
    return ceiling


def request_allowance(body: Mapping[str, Any] | None, ceiling: int) -> int:
    """What a request whose completion tokens were not learned counts as: the larger token field it names, else the
    ceiling."""
    named = [v for v in (_int((body or {}).get(k)) for k in ('max_tokens', 'max_completion_tokens')) if v]
    return max(named) if named else ceiling


# ---------------------------------------------------------------- reading ---------------------------------------------


def parse_prometheus(text: str) -> list[tuple[str, dict[str, str], float]]:
    """Prometheus text exposition: ``(name, labels, value)`` per sample line; comments and odd lines skipped."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        m = _SERIES.match(line)
        if not m:
            continue
        try:
            value = float(m.group(4))
        except ValueError:
            continue
        labels = {k: v.replace('\\"', '"').replace('\\\\', '\\') for k, v in _LABEL.findall(m.group(3) or '')}
        out.append((m.group(1), labels, value))
    return out


def runtime_counters(text: str, table: Mapping[str, tuple[str, Mapping[str, str]]]) -> dict[str, float] | None:
    """The counters ``table`` names (``RUNTIME_COUNTERS[runtime]``), each the sum of the series that match; None when
    the completion-token series is absent (nothing to judge by)."""
    samples = parse_prometheus(text)
    out: dict[str, float] = {}
    for key, (name, labels) in table.items():
        matched = [v for n, ls, v in samples if n == name and all(ls.get(k) == want for k, want in labels.items())]
        if matched:
            out[key] = sum(matched)
    return out if 'completion_tokens' in out else None


@dataclass(frozen=True)
class Served:
    """One instance's totals in the gateway's ``/healthz`` ``served``, since the gateway started."""

    requests: int = 0
    completion_tokens: int = 0
    unaccounted_requests: int = 0
    unaccounted_allowance_tokens: int = 0
    decode_tps_alone_p50: float | None = None
    decode_tps_alone_n: int = 0

    @classmethod
    def from_dict(cls, d: Any) -> Served:
        d = d if isinstance(d, dict) else {}
        return cls(
            requests=_int(d.get('requests')) or 0,
            completion_tokens=_int(d.get('completion_tokens')) or 0,
            unaccounted_requests=_int(d.get('unaccounted_requests')) or 0,
            unaccounted_allowance_tokens=_int(d.get('unaccounted_allowance_tokens')) or 0,
            decode_tps_alone_p50=_number(d.get('decode_tps_alone_p50')),
            decode_tps_alone_n=_int(d.get('decode_tps_alone_n')) or 0,
        )


@dataclass(frozen=True)
class GatewayView:
    """One read of the gateway's ``/healthz``: when it started and what it has served each instance."""

    started_at: float
    served: dict[str, Served] = field(default_factory=dict)
    in_flight: dict[str, int] = field(default_factory=dict)

    def of(self, instance_id: str) -> Served:
        return self.served.get(instance_id, Served())


def gateway_view(doc: Any) -> GatewayView | None:
    """None when the read failed or the gateway keeps no totals (an older gateway): no judgement from it."""
    if not isinstance(doc, dict):
        return None
    started_at, served = _number(doc.get('started_at')), doc.get('served')
    if started_at is None or not isinstance(served, dict):
        return None
    in_flight = doc.get('in_flight')
    if not isinstance(in_flight, dict):
        in_flight = {}
    return GatewayView(
        started_at,
        {str(k): Served.from_dict(v) for k, v in served.items()},
        {str(k): n for k, v in in_flight.items() if (n := _int(v)) is not None},
    )


# ---------------------------------------------------------------- the judgement ---------------------------------------


@dataclass(frozen=True)
class Baseline:
    at: float
    runtime_completion: float
    gateway_started_at: float
    gateway_completion: int
    unaccounted_allowance: int


@dataclass(frozen=True)
class Track:
    """What the check keeps per leased instance between visits (in memory: a restarted controller starts over)."""

    baseline: Baseline | None = None
    last_runtime: float | None = None
    strikes: int = 0
    silent: str = ''  # the no-judgement reason last logged: said once, not every visit


@dataclass(frozen=True)
class Sample:
    """One visit's reads for one instance. ``counters`` None: the runtime's could not be read (``why`` says so);
    ``before`` / ``after``: the gateway's reads either side of them."""

    at: float
    counters: dict[str, float] | None
    before: GatewayView | None
    after: GatewayView | None
    ceiling: int = cfg.RUNTIME_OUTPUT_CEILING_TOKENS
    why: str = ''


@dataclass(frozen=True)
class Judgement:
    kind: str
    detail: str = ''
    numbers: dict[str, Any] = field(default_factory=dict)
    log: bool = True  # a no-judgement repeats its reason silently

    @property
    def detected(self) -> bool:
        return self.kind == DETECTION


def _no_judgement(track: Track, why: str) -> tuple[Track, Judgement]:
    return replace(track, strikes=0, silent=why), Judgement(NO_JUDGEMENT, why, log=why != track.silent)


def _baseline(sample: Sample, runtime: float, before: GatewayView, instance_id: str) -> Baseline:
    served = before.of(instance_id)
    return Baseline(
        sample.at, runtime, before.started_at, served.completion_tokens, served.unaccounted_allowance_tokens
    )


def judge(
    track: Track,
    sample: Sample,
    instance_id: str,
    min_tokens: int = cfg.EXTERNAL_USE_MIN_TOKENS,
    min_fraction: float = cfg.EXTERNAL_USE_MIN_FRACTION,
    strikes_needed: int = cfg.EXTERNAL_USE_STRIKES,
) -> tuple[Track, Judgement]:
    """One visit's judgement for one instance: the track to keep and what to log (and act on, for a detection)."""
    if sample.counters is None:
        return _no_judgement(track, sample.why or 'runtime counters not read')
    if sample.before is None or sample.after is None:
        return _no_judgement(track, sample.why or 'gateway totals not read')
    runtime = float(sample.counters['completion_tokens'])
    before, after = sample.before, sample.after
    evidence = {
        'runtime_completion_tokens': runtime,
        **{f'runtime_{k}': v for k, v in sample.counters.items() if k != 'completion_tokens'},
        'gateway_started_at': after.started_at,
    }
    if before.started_at != after.started_at:  # the gateway restarted between the two reads
        track = Track(None, runtime, 0, '')
        return track, Judgement(
            REBASELINE, 'the gateway restarted mid-sample: new baseline at the next visit', evidence
        )
    if track.baseline is None:
        track = Track(_baseline(sample, runtime, before, instance_id), runtime, 0, '')
        return track, Judgement(BASELINE, 'baseline', evidence)
    if track.last_runtime is not None and runtime < track.last_runtime:
        track = Track(_baseline(sample, runtime, before, instance_id), runtime, 0, '')
        return track, Judgement(REBASELINE, 'the runtime counter went down (runtime restarted): new baseline', evidence)
    if before.started_at != track.baseline.gateway_started_at:
        track = Track(_baseline(sample, runtime, before, instance_id), runtime, 0, '')
        return track, Judgement(REBASELINE, 'the gateway restarted: new baseline', evidence)

    base, served = track.baseline, after.of(instance_id)
    d_runtime = runtime - base.runtime_completion
    d_gateway = served.completion_tokens - base.gateway_completion
    d_allowance = served.unaccounted_allowance_tokens - base.unaccounted_allowance
    in_flight = max(0, after.in_flight.get(instance_id, 0))
    surplus = d_runtime - d_gateway - d_allowance - in_flight * sample.ceiling
    threshold = max(float(min_tokens), min_fraction * d_runtime)
    numbers = {
        **evidence,
        'since': base.at,
        'runtime_delta': d_runtime,
        'gateway_delta': d_gateway,
        'unaccounted_allowance_delta': d_allowance,
        'in_flight': in_flight,
        'ceiling': sample.ceiling,
        'surplus': surplus,
        'threshold': threshold,
        'gateway_requests': served.requests,
        'gateway_unaccounted_requests': served.unaccounted_requests,
    }
    if surplus <= threshold:
        return Track(base, runtime, 0, ''), Judgement(CLEAR, 'within bounds', numbers)
    strikes = track.strikes + 1
    numbers['strikes'] = strikes
    detail = f'surplus {surplus:.0f} tokens over {threshold:.0f} ({strikes}/{strikes_needed})'
    kind = DETECTION if strikes >= strikes_needed else STRIKE
    return Track(base, runtime, strikes, ''), Judgement(kind, detail, numbers)


def throughput_evidence(
    served: Served,
    qualified_tps: Any,
    fraction: float = cfg.THROUGHPUT_EVIDENCE_FRACTION,
    min_n: int = cfg.THROUGHPUT_EVIDENCE_MIN_N,
) -> dict[str, Any] | None:
    """Recorded only: the gateway's median decode rate over requests that ran alone on the card, when it is below
    ``fraction`` of the manifest's ``profile.decode_tps_single`` over at least ``min_n`` requests. None otherwise."""
    single, p50 = _number(qualified_tps), served.decode_tps_alone_p50
    if single is None or single <= 0 or p50 is None or served.decode_tps_alone_n < min_n:
        return None
    if p50 >= fraction * single:
        return None
    return {
        'decode_tps_alone_p50': p50,
        'decode_tps_alone_n': served.decode_tps_alone_n,
        'decode_tps_single': single,
        'fraction': round(p50 / single, 3),
    }


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None
