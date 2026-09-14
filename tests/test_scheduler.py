import pytest

from simulator.clock import utc
from simulator.rng import RandomSource
from simulator.scheduler import (
    FLAT,
    EventCalendar,
    arrivals,
    intensity_at,
    jittered,
    poisson,
    validate_curve,
)

#: A shop's day, declared here the way a pack file would declare it:
#: dead overnight, a lunch bump, an evening peak, tapering after close.
#: It lives in the test rather than in the engine because the engine has
#: no business knowing what any particular domain's day looks like.
TRADING_DAY = (
    0.02, 0.01, 0.01, 0.01, 0.02, 0.08, 0.25, 0.55,
    0.80, 0.95, 1.00, 1.10, 1.35, 1.20, 1.00, 1.05,
    1.20, 1.40, 1.25, 0.90, 0.55, 0.30, 0.12, 0.05,
)


def test_events_pop_in_time_order_not_insertion_order():
    calendar = EventCalendar()
    calendar.schedule(utc(2026, 1, 3), "late")
    calendar.schedule(utc(2026, 1, 1), "early")
    calendar.schedule(utc(2026, 1, 2), "middle")
    due = calendar.pop_due(utc(2026, 1, 4))
    assert [event.kind for event in due] == ["early", "middle", "late"]


def test_simultaneous_events_with_dict_payloads_do_not_raise():
    # Without ScheduledEvent.sequence the heap falls through to
    # comparing `kind` and then `payload`, and comparing two dicts
    # raises TypeError. Two events on one timestamp is rare enough to
    # survive a short run, so this is the control that pins it.
    calendar = EventCalendar()
    moment = utc(2026, 1, 1, 9)
    calendar.schedule(moment, "restock", {"sku": "A"})
    calendar.schedule(moment, "restock", {"sku": "B"})
    due = calendar.pop_due(moment)
    assert [event.payload["sku"] for event in due] == ["A", "B"]


def test_pop_due_leaves_the_future_alone():
    calendar = EventCalendar()
    calendar.schedule(utc(2026, 1, 1, 10), "now")
    calendar.schedule(utc(2026, 1, 1, 14), "later")
    assert len(calendar.pop_due(utc(2026, 1, 1, 11))) == 1
    assert len(calendar) == 1
    assert calendar.peek_next().kind == "later"


def test_events_scheduled_while_handling_are_not_swallowed():
    # Callers routinely schedule during handling -- a restock that
    # triggers a reorder. Those must surface on the NEXT call, not be
    # lost and not be handled mid-iteration.
    calendar = EventCalendar()
    calendar.schedule(utc(2026, 1, 1, 9), "first")
    due = calendar.pop_due(utc(2026, 1, 1, 12))
    assert len(due) == 1
    calendar.schedule(utc(2026, 1, 1, 10), "scheduled-during-handling")
    assert [e.kind for e in calendar.pop_due(utc(2026, 1, 1, 12))] == ["scheduled-during-handling"]


def test_intensity_follows_the_hour():
    quiet = intensity_at(utc(2026, 1, 1, 3), TRADING_DAY)
    busy = intensity_at(utc(2026, 1, 1, 17), TRADING_DAY)
    assert quiet < 0.1
    assert busy > 1.0


def test_intensity_rejects_a_malformed_curve():
    with pytest.raises(ValueError, match="24 entries"):
        intensity_at(utc(2026, 1, 1), (1.0, 1.0))


def test_poisson_mean_is_approximately_right():
    rng = RandomSource(42).stream("poisson")
    draws = [poisson(rng, 4.0) for _ in range(5000)]
    assert sum(draws) / len(draws) == pytest.approx(4.0, abs=0.2)


def test_poisson_edges():
    rng = RandomSource(1).stream("p")
    assert poisson(rng, 0) == 0
    with pytest.raises(ValueError, match="non-negative"):
        poisson(rng, -1)
    with pytest.raises(ValueError, match="too large"):
        poisson(rng, 5000)


def test_arrivals_are_busier_at_peak_than_overnight():
    # The property that makes the data believable, asserted directly:
    # the same rate and the same interval must produce far more
    # arrivals at 5pm than at 3am. A flat-rate implementation gives
    # roughly equal totals here and fails.
    rng = RandomSource(8).stream("arrivals")
    overnight = sum(arrivals(rng, utc(2026, 1, 1, 3), 3600, 10.0, TRADING_DAY) for _ in range(200))
    peak = sum(arrivals(rng, utc(2026, 1, 1, 17), 3600, 10.0, TRADING_DAY) for _ in range(200))
    assert peak > overnight * 5


def test_arrivals_scale_with_interval_length():
    rng = RandomSource(9).stream("a")
    short = sum(arrivals(rng, utc(2026, 1, 1, 12), 600, 60.0) for _ in range(300))
    long = sum(arrivals(rng, utc(2026, 1, 1, 12), 3600, 60.0) for _ in range(300))
    assert long > short * 4


def test_arrivals_are_zero_for_empty_intervals():
    rng = RandomSource(2).stream("a")
    assert arrivals(rng, utc(2026, 1, 1, 12), 0, 10.0) == 0
    assert arrivals(rng, utc(2026, 1, 1, 12), 600, 0) == 0


def test_jittered_stays_within_its_spread():
    rng = RandomSource(4).stream("j")
    values = [jittered(rng, 100.0, 0.2) for _ in range(500)]
    assert all(80.0 <= value <= 120.0 for value in values)
    # And actually varies -- a no-op implementation returning `base`
    # would satisfy the bounds above and nothing else.
    assert len(set(values)) > 400


def test_jittered_rejects_an_out_of_range_spread():
    rng = RandomSource(4).stream("j")
    with pytest.raises(ValueError, match="spread must be"):
        jittered(rng, 100.0, 1.0)


def test_validate_curve_accepts_a_real_curve():
    assert validate_curve("trading_day", list(TRADING_DAY)) == TRADING_DAY


def test_validate_curve_rejects_the_wrong_length():
    # A curve with twenty-three entries is a typo in a YAML file, and the
    # useful place to say so is before any database exists.
    with pytest.raises(ValueError, match="24 hourly weights, got 23"):
        validate_curve("trading_day", list(TRADING_DAY)[:23])


def test_validate_curve_rejects_a_negative_weight():
    broken = list(TRADING_DAY)
    broken[3] = -1.0
    with pytest.raises(ValueError, match="negative weight .* at hour 3"):
        validate_curve("trading_day", broken)


def test_validate_curve_rejects_an_all_zero_curve():
    # Not a quiet domain -- a curve that can never produce an arrival.
    # Silent otherwise: the pack would simply do nothing, forever.
    with pytest.raises(ValueError, match="zero at every hour"):
        validate_curve("dead", [0.0] * 24)


def test_the_flat_default_is_usable_but_flat():
    # FLAT exists so a pack declaring a rate and no curve still runs. It
    # is deliberately the worst realistic choice, and this pins that it
    # really is flat rather than quietly shaped.
    assert len(set(FLAT)) == 1
    rng = RandomSource(3).stream("flat")
    overnight = sum(arrivals(rng, utc(2026, 1, 1, 3), 3600, 10.0, FLAT) for _ in range(200))
    peak = sum(arrivals(rng, utc(2026, 1, 1, 17), 3600, 10.0, FLAT) for _ in range(200))
    assert 0.8 < peak / overnight < 1.25
