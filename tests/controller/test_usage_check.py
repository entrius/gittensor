# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The lease accounting check, pure: the runtime's counters parsed, the gateway's totals read, the baseline and the
deltas, both re-baseline cases, the unaccounted allowance and in-flight bounds, a strike that clears, two that detect,
no judgement (and never a strike) when the runtime, its counters or the gateway cannot be read, and the throughput
evidence. Then the box side: one SOFT ``external_use`` event with the reason, the cooldown, and the third inside a
week benching at the 16 h rung (a fourth at 64 h)."""

import pytest

from gittensor.controller.checks import config as cfg
from gittensor.controller.checks.state import (
    BENCHED,
    EXTERNAL_USE,
    IDLE,
    LEASED,
    BoxState,
    CardState,
    apply_external_use,
    external_uses,
    lease_cooldown_until,
)
from gittensor.controller.manifest import parse_manifest
from gittensor.controller.standing import SOFT, STANDARD, TRUSTED, standing
from gittensor.controller.usage_check import (
    BASELINE,
    CLEAR,
    DETECTION,
    NO_JUDGEMENT,
    REBASELINE,
    STRIKE,
    GatewayView,
    Sample,
    Served,
    Track,
    gateway_view,
    judge,
    output_ceiling,
    parse_prometheus,
    request_allowance,
    runtime_counters,
    throughput_evidence,
)
from tests.controller.test_placement import placeholder_doc

SPARK = cfg.RUNTIME_COUNTERS['sparkinfer']
INST = 'i-abc123'
CEILING = cfg.RUNTIME_OUTPUT_CEILING_TOKENS

METRICS = """# HELP sparkinfer_tokens_total Tokens processed.
# TYPE sparkinfer_tokens_total counter
sparkinfer_tokens_total{kind="prompt",model="qwen3.8-27b"} 91234
sparkinfer_tokens_total{kind="completion",model="qwen3.8-27b"} 5000
sparkinfer_tokens_total{kind="completion",model="draft"} 250
sparkinfer_requests_total 17
sparkinfer_active_requests 1
"""


def gw(started: float = 100.0, /, *, in_flight=None, **served) -> GatewayView:
    """A gateway read: ``served`` as instance -> (completion tokens, unaccounted allowance) or a Served."""
    rows = {k: v if isinstance(v, Served) else Served(completion_tokens=v[0], unaccounted_allowance_tokens=v[1]) for k, v in served.items()}  # fmt: skip
    return GatewayView(started, rows, dict(in_flight or {}))


def sample(runtime, before=None, after=None, at=0.0, **kw) -> Sample:
    before = before if before is not None else gw()
    after = after if after is not None else before
    counters = None if runtime is None else {'completion_tokens': float(runtime)}
    return Sample(at, counters, before, after, kw.pop('ceiling', CEILING), **kw)


def run(*samples: Sample, track: Track | None = None):
    """Judge samples in order; returns the last track and every judgement."""
    track = track or Track()
    out = []
    for s in samples:
        track, j = judge(track, s, INST)
        out.append(j)
    return track, out


# ---------------------------------------------------------------- reading ---------------------------------------------


def test_the_runtime_counters_sum_the_series_that_match_and_need_the_completion_series():
    assert ('sparkinfer_requests_total', {}, 17.0) in parse_prometheus(METRICS)
    counters = runtime_counters(METRICS, SPARK)
    assert counters == {'completion_tokens': 5250.0, 'prompt_tokens': 91234.0, 'requests': 17.0, 'active': 1.0}
    assert (
        runtime_counters('sparkinfer_requests_total 17\n', SPARK) is None
    )  # no completion series: nothing to judge by
    assert runtime_counters('<html>not metrics</html>', SPARK) is None
    assert runtime_counters('sparkinfer_tokens_total{kind="completion"} NaN-ish\n', SPARK) is None


def test_the_gateway_read_needs_its_start_time_and_its_totals():
    assert gateway_view(None) is None
    assert gateway_view({'status': 'ok', 'in_flight': {}}) is None  # a gateway that keeps no totals
    doc = {
        'started_at': 50.0,
        'in_flight': {INST: 1},
        'served': {INST: {'requests': 3, 'completion_tokens': 900, 'unaccounted_requests': 1, 'unaccounted_allowance_tokens': 256}},
    }  # fmt: skip
    view = gateway_view(doc)
    assert view is not None and view.started_at == 50.0 and view.in_flight == {INST: 1}
    assert view.of(INST) == Served(3, 900, 1, 256) and view.of('i-other') == Served()


def test_the_ceiling_is_the_runtime_default_or_a_higher_limit_the_manifest_names():
    assert output_ceiling(None) == CEILING
    plain = parse_manifest(placeholder_doc(), allow_placeholder_digest=True)
    assert output_ceiling(plain) == CEILING
    raised = placeholder_doc()
    raised['run']['env']['SPARKINFER_MAX_OUTPUT_TOKENS'] = '32768'
    assert output_ceiling(parse_manifest(raised, allow_placeholder_digest=True)) == 32768
    lower = placeholder_doc()
    lower['run']['env']['SPARKINFER_MAX_OUTPUT_TOKENS'] = '4096'
    assert (
        output_ceiling(parse_manifest(lower, allow_placeholder_digest=True)) == CEILING
    )  # never below the default: an upper bound
    assert request_allowance({'max_tokens': 256}, CEILING) == 256
    assert request_allowance({'max_tokens': 256, 'max_completion_tokens': 900}, CEILING) == 900
    assert request_allowance({'max_tokens': True}, CEILING) == CEILING
    assert request_allowance(None, CEILING) == CEILING


# ---------------------------------------------------------------- the judgement ---------------------------------------


def test_the_first_sample_is_the_baseline_and_honest_traffic_stays_clear():
    base = gw(**{INST: (4000, 0)})
    track, (first,) = run(sample(1000, base))
    assert first.kind == BASELINE and track.baseline is not None and track.baseline.runtime_completion == 1000
    # 30,000 tokens through the gateway, the runtime made exactly those.
    track, (second,) = run(sample(31_000, gw(**{INST: (34_000, 0)})), track=track)
    assert second.kind == CLEAR and second.numbers['surplus'] == 0 and track.strikes == 0
    assert second.numbers['runtime_delta'] == 30_000 and second.numbers['gateway_delta'] == 30_000


def test_the_start_canary_before_the_baseline_is_not_counted():
    # The runtime already counted the canary's tokens (64) before the lease: the baseline absorbs them.
    _, (base, later) = run(sample(64, gw()), sample(64, gw()))
    assert base.kind == BASELINE and later.kind == CLEAR and later.numbers['surplus'] == 0


def test_one_strike_then_clean_clears_and_two_in_a_row_detect():
    base = sample(0, gw())
    track, (_, s1) = run(base, sample(10_000, gw(**{INST: (0, 0)})))
    assert s1.kind == STRIKE and track.strikes == 1 and s1.numbers['surplus'] == 10_000
    # The gateway catches up on the next visit (the tokens were ours after all): the count clears.
    track, (cleared,) = run(sample(10_000, gw(**{INST: (10_000, 0)})), track=track)
    assert cleared.kind == CLEAR and track.strikes == 0
    track, (s2,) = run(sample(20_000, gw(**{INST: (10_000, 0)})), track=track)
    track, (s3,) = run(sample(30_000, gw(**{INST: (10_000, 0)})), track=track)
    assert (s2.kind, s3.kind) == (STRIKE, DETECTION) and s3.detected and s3.numbers['strikes'] == 2


def test_the_threshold_is_the_larger_of_the_floor_and_the_fraction():
    # Small leases: up to 2000 tokens of surplus is never a strike.
    _, (_, j) = run(sample(0), sample(2000, gw(**{INST: (0, 0)})))
    assert j.kind == CLEAR and j.numbers['threshold'] == 2000
    _, (_, j) = run(sample(0), sample(2001, gw(**{INST: (0, 0)})))
    assert j.kind == STRIKE
    # Busy leases: 5 % of what the runtime made.
    _, (_, j) = run(sample(0), sample(1_000_000, gw(**{INST: (951_000, 0)})))
    assert j.kind == CLEAR and j.numbers['threshold'] == 50_000 and j.numbers['surplus'] == 49_000
    _, (_, j) = run(sample(0), sample(1_000_000, gw(**{INST: (949_000, 0)})))
    assert j.kind == STRIKE


def test_unaccounted_requests_count_at_their_allowance():
    # Three streams without usage (clients left, no include_usage): each counts as its max_tokens (4096).
    _, (_, j) = run(sample(0), sample(3 * 4096, gw(**{INST: (0, 3 * 4096)})))
    assert j.kind == CLEAR and j.numbers['unaccounted_allowance_delta'] == 3 * 4096 and j.numbers['surplus'] == 0
    # A request that stopped early made fewer than its allowance: the difference is in the miner's favour.
    _, (_, j) = run(sample(0), sample(300, gw(**{INST: (0, 4096)})))
    assert j.kind == CLEAR and j.numbers['surplus'] < 0


def test_requests_in_flight_count_at_the_ceiling():
    # A long answer still streaming at the sample: the runtime has made its tokens, the gateway has not counted them.
    track, _ = run(sample(0))
    after = gw(in_flight={INST: 1}, **{INST: (0, 0)})
    _, (j,) = run(sample(CEILING, gw(), after), track=track)
    assert j.kind == CLEAR and j.numbers['in_flight'] == 1 and j.numbers['surplus'] == 0
    raised = sample(20_000, gw(), gw(in_flight={INST: 1}), ceiling=32768)  # a manifest with a higher runtime limit
    _, (j,) = run(raised, track=track)
    assert j.kind == CLEAR


def test_a_request_that_finishes_between_the_two_gateway_reads_is_never_lost():
    track, _ = run(sample(0, gw(**{INST: (0, 0)})))
    # Before the runtime read, the request is in flight; after it, the gateway has counted its 8000 tokens. The runtime
    # read (between the two) already has them: the after-read covers them.
    before = gw(in_flight={INST: 1}, **{INST: (0, 0)})
    after = gw(**{INST: (8000, 0)})
    _, (j,) = run(sample(8000, before, after), track=track)
    assert j.kind == CLEAR and j.numbers['surplus'] == 0


def test_the_baseline_takes_the_gateway_read_before_the_runtime():
    # A request finishing between the baseline's two reads: the runtime already counted it at baseline, and so does
    # the before-read's total... unless it finished after it; then the gateway's delta counts it and the runtime's
    # does not: in the miner's favour, never against.
    before, after = gw(**{INST: (0, 0)}), gw(**{INST: (8000, 0)})
    track, (base,) = run(sample(8000, before, after))
    assert base.kind == BASELINE and track.baseline is not None and track.baseline.gateway_completion == 0
    _, (j,) = run(sample(8000, after), track=track)
    assert j.kind == CLEAR and j.numbers['surplus'] == -8000


def test_a_runtime_counter_that_went_down_starts_a_new_baseline_and_clears_strikes():
    track, _ = run(sample(0), sample(10_000, gw(**{INST: (0, 0)})))
    assert track.strikes == 1
    track, (j,) = run(sample(50, gw(**{INST: (0, 0)})), track=track)  # the runtime restarted: its counters start over
    assert (
        j.kind == REBASELINE
        and track.strikes == 0
        and track.baseline is not None
        and track.baseline.runtime_completion == 50
    )
    track, (j,) = run(sample(50, gw(**{INST: (0, 0)})), track=track)
    assert j.kind == CLEAR


def test_a_gateway_restart_starts_a_new_baseline_and_clears_strikes():
    track, _ = run(sample(0, gw(100.0, **{INST: (0, 0)})), sample(10_000, gw(100.0, **{INST: (0, 0)})))
    assert track.strikes == 1
    # The gateway restarted: its totals start over at zero, the runtime's keep counting.
    track, (j,) = run(sample(20_000, gw(200.0, **{INST: (500, 0)})), track=track)
    assert (
        j.kind == REBASELINE
        and track.strikes == 0
        and track.baseline is not None
        and track.baseline.gateway_started_at == 200.0
    )
    track, (j,) = run(sample(21_000, gw(200.0, **{INST: (1500, 0)})), track=track)
    assert j.kind == CLEAR and j.numbers['surplus'] == 0
    # A restart between the two reads of one sample: no baseline from it, the next visit takes one.
    track, (j,) = run(sample(30_000, gw(200.0), gw(300.0)), track=track)
    assert j.kind == REBASELINE and track.baseline is None and track.strikes == 0
    track, (j,) = run(sample(30_000, gw(300.0)), track=track)
    assert j.kind == BASELINE


@pytest.mark.parametrize(
    'broken, why',
    [
        (dict(runtime=None, why="runtime 'vllm' has no counters table"), "runtime 'vllm' has no counters table"),
        (dict(runtime=None, why='/metrics -> 404'), '/metrics -> 404'),
        (dict(runtime=None), 'runtime counters not read'),
        (dict(runtime=99_000, before=None, after=None, why='no gateway to ask'), 'no gateway to ask'),
    ],
)
def test_nothing_readable_is_no_judgement_logged_once_and_never_a_strike(broken, why):
    track, _ = run(sample(0), sample(10_000, gw(**{INST: (0, 0)})))
    assert track.strikes == 1
    runtime = broken.pop('runtime')
    s = Sample(0.0, None if runtime is None else {'completion_tokens': runtime}, **{'before': gw(), 'after': gw(), **broken})  # fmt: skip
    track, (j,) = run(s, track=track)
    assert j.kind == NO_JUDGEMENT and j.detail == why and j.log and track.strikes == 0
    track, (again,) = run(s, track=track)
    assert again.kind == NO_JUDGEMENT and not again.log  # said once
    track, (j,) = run(sample(10_000, gw(**{INST: (10_000, 0)})), track=track)  # readable again: the baseline held
    assert j.kind == CLEAR and track.silent == ''


def test_an_unreadable_gateway_read_after_the_runtime_is_no_judgement():
    track, _ = run(sample(0))
    _, (j,) = run(Sample(0.0, {'completion_tokens': 99_000.0}, gw(), None, CEILING), track=track)
    assert j.kind == NO_JUDGEMENT and j.detail == 'gateway totals not read'


def test_every_judgement_carries_the_numbers_for_the_audit():
    counters = runtime_counters(METRICS, SPARK)
    track, _ = run(sample(0))
    _, j = judge(track, Sample(7.0, counters, gw(), gw(**{INST: (5250, 0)}), CEILING), INST)
    for key in (
        'runtime_completion_tokens', 'runtime_prompt_tokens', 'runtime_requests', 'runtime_active', 'since',
        'runtime_delta', 'gateway_delta', 'unaccounted_allowance_delta', 'in_flight', 'ceiling', 'surplus',
        'threshold', 'gateway_requests', 'gateway_unaccounted_requests', 'gateway_started_at',
    ):  # fmt: skip
        assert key in j.numbers, key


def test_throughput_evidence_is_below_the_fraction_over_enough_requests():
    low = Served(decode_tps_alone_p50=40.0, decode_tps_alone_n=25)
    assert throughput_evidence(low, 92) == {
        'decode_tps_alone_p50': 40.0,
        'decode_tps_alone_n': 25,
        'decode_tps_single': 92.0,
        'fraction': 0.435,
    }
    assert throughput_evidence(Served(decode_tps_alone_p50=60.0, decode_tps_alone_n=25), 92) is None  # 0.65
    assert throughput_evidence(Served(decode_tps_alone_p50=40.0, decode_tps_alone_n=19), 92) is None  # too few
    assert throughput_evidence(low, None) is None  # no qualified rate in the manifest
    assert throughput_evidence(Served(), 92) is None


# ---------------------------------------------------------------- the box ---------------------------------------------


def leased_box(events=(), bench_count=0, benched_at=None, admitted_at=None) -> BoxState:
    uuid = 'GPU-1'
    return BoxState(
        'hk1',
        status=IDLE,
        pinned_uuids=[uuid],
        cards={uuid: CardState(LEASED, INST, 0.0)},
        standing_events=list(events),
        bench_count=bench_count,
        benched_at=benched_at,
        admitted_at=admitted_at,
    )


def use(at):
    return {'at': at, 'kind': EXTERNAL_USE, 'uuid': 'GPU-1', 'reason': cfg.EXTERNAL_USE_REASON}


def test_a_detection_is_one_soft_event_with_the_reason_and_leaves_the_card_to_the_planned_drain():
    assert EXTERNAL_USE in SOFT
    trusted = leased_box([{'at': 1.0, 'kind': 'clean_lease', 'leased_s': cfg.STANDING_TRUSTED_AFTER_S}])
    assert standing(trusted.standing_events) == TRUSTED
    after = apply_external_use(trusted, 'GPU-1', 1000.0, instance=INST, surplus=12_345.0)
    event = after.standing_events[-1]
    assert event['kind'] == EXTERNAL_USE and event['reason'] == 'suspected external (non-gateway) usage'
    assert event['surplus'] == 12_345.0 and event['instance'] == INST
    assert standing(after.standing_events) == STANDARD  # one level down
    assert after.status == IDLE and after.cards['GPU-1'].state == LEASED  # no bench, the drain moves the card
    assert after.withheld_from is None and after.bench_count == 0


def test_the_cooldown_runs_from_the_last_external_use_event():
    assert lease_cooldown_until(leased_box()) is None
    box = leased_box([use(1000.0), use(5000.0)])
    assert lease_cooldown_until(box) == 5000.0 + cfg.EXTERNAL_USE_COOLDOWN_S


def test_the_third_inside_a_week_benches_at_the_16_hour_rung_and_a_fourth_at_64():
    day = 86_400.0
    now = 10 * day
    box = apply_external_use(leased_box([use(now - 3 * day), use(now - day)]), 'GPU-1', now)
    assert box.status == BENCHED and box.last_failed == [EXTERNAL_USE]
    assert box.bench_until == now + 57_600 and box.bench_count == 3  # 16 h, whatever the count was
    assert box.withheld_from is None and box.cards == {}
    # Back through ADMIT, leased again, a fourth inside the week: 64 h.
    back = BoxState.from_dict({**box.as_dict(), 'status': IDLE, 'cards': {'GPU-1': {'state': LEASED}}})
    later = apply_external_use(back, 'GPU-1', now + day)
    assert later.status == BENCHED and later.bench_until == now + day + 230_400 and later.bench_count == 4


def test_the_bench_floor_holds_after_clean_time_has_cleared_the_ladder():
    day = 86_400.0
    now = 30 * day
    old_bench = leased_box(
        [use(now - 2 * day), use(now - day)], bench_count=4, benched_at=now - 20 * day, admitted_at=now - 19 * day
    )
    box = apply_external_use(old_bench, 'GPU-1', now)
    assert box.bench_count == 3 and box.bench_until == now + 57_600  # clean time cleared the rungs, not the floor


def test_events_older_than_a_week_do_not_count():
    day = 86_400.0
    now = 20 * day
    box = leased_box([use(now - 8 * day), use(now - 7 * day), use(now - 2 * day)])
    assert len(external_uses(box, now)) == 1
    after = apply_external_use(box, 'GPU-1', now)
    assert after.status == IDLE and len(external_uses(after, now)) == 2
