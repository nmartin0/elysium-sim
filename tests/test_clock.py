from datetime import datetime, timedelta

import pytest

from simulator.clock import DEFAULT_COMPRESSION, SimulatedClock, day_fraction, utc


def test_naive_start_is_rejected():
    # Naive datetimes compare and subtract in ways that look correct
    # and are not once a source reports an offset, so this is a hard
    # error rather than a silent coercion to UTC.
    with pytest.raises(ValueError, match="timezone-aware"):
        SimulatedClock(start=datetime(2026, 1, 1))


def test_non_positive_compression_is_rejected():
    with pytest.raises(ValueError, match="compression must be positive"):
        SimulatedClock(start=utc(2026, 1, 1), compression=0)


def test_advance_moves_simulated_time_only():
    clock = SimulatedClock(start=utc(2026, 1, 1, 9))
    assert clock.now() == utc(2026, 1, 1, 9)
    clock.advance(3600)
    assert clock.now() == utc(2026, 1, 1, 10)
    assert clock.elapsed == timedelta(hours=1)


def test_advance_real_applies_compression():
    clock = SimulatedClock(start=utc(2026, 1, 1), compression=DEFAULT_COMPRESSION)
    # One real second buys one simulated minute at the default factor.
    clock.advance_real(1.0)
    assert clock.now() == utc(2026, 1, 1, 0, 1)


def test_real_seconds_for_inverts_advance_real():
    clock = SimulatedClock(start=utc(2026, 1, 1), compression=60.0)
    # A full simulated day should cost 24 real minutes at this factor,
    # which is the claim the module docstring makes.
    assert clock.real_seconds_for(86400) == pytest.approx(24 * 60)


def test_time_cannot_run_backwards():
    # Every pack builds append-only ledgers; a clock that rewound
    # would corrupt them. Backfill runs forward from an earlier start.
    clock = SimulatedClock(start=utc(2026, 1, 1))
    with pytest.raises(ValueError, match="negative interval"):
        clock.advance(-1)


def test_day_fraction_spans_the_day():
    assert day_fraction(utc(2026, 1, 1, 0, 0)) == 0.0
    assert day_fraction(utc(2026, 1, 1, 12, 0)) == pytest.approx(0.5)
    assert day_fraction(utc(2026, 1, 1, 18, 0)) == pytest.approx(0.75)


def test_jumping_forward_is_equivalent_to_many_small_steps():
    # The property that makes "show me month-end" cheap: a single
    # large advance must land where repeated small ones would.
    stepwise = SimulatedClock(start=utc(2026, 3, 1))
    for _ in range(720):
        stepwise.advance(3600)
    jumped = SimulatedClock(start=utc(2026, 3, 1))
    jumped.advance(720 * 3600)
    assert stepwise.now() == jumped.now()
