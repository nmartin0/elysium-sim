"""
clock.py  (simulated time, and why nothing here calls datetime.now())

Every timestamp the simulator produces comes from this clock. Not one
of them comes from the wall. That is not tidiness: it is what makes
three separate things possible at once.

  - reproducibility. A run seeded the same way and started at the same
    simulated instant produces byte-identical databases. A run that
    mixed in the wall clock could never be replayed, so a drift bug
    found on Tuesday could not be reproduced on Wednesday.
  - backfill shares one code path with forward simulation. History is
    not a separate generator writing plausible-looking old rows; it is
    this same engine run from an earlier start at maximum speed. A
    separate historical generator is the classic way synthetic data
    goes wrong -- the past stops looking like the present, because two
    bodies of code drifted.
  - jumping. "Show me month-end" is advance(hours=720), applying every
    intervening event, rather than waiting.

Compression, and why one simulated minute per real second is the
default. Faster and a human cannot watch an entity move through its
states and see where it went wrong; slower and a full business day
does not fit in a coffee break. At this factor a business day takes 24
real minutes and a single flight leg about two.

The clock does not sleep or own a thread. It is advanced by whoever is
driving -- a test advancing deterministically, or a run loop sleeping
between ticks. Keeping the waiting outside means the same clock serves
a millisecond-fast test and a live demo without a mode flag.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: One real second becomes one simulated minute. See the module note.
DEFAULT_COMPRESSION = 60.0


@dataclass
class SimulatedClock:
    """The simulator's only source of "now".

    Mutable by design, unlike most of the value types here: a clock
    whose whole purpose is to advance would gain nothing from being
    frozen, and every caller shares one instance deliberately so that
    two subsystems can never disagree about the time.
    """

    #: Where simulated time starts. Timezone-aware, always UTC --
    #: naive datetimes compare and subtract in ways that look right
    #: and silently are not once a source system reports an offset.
    start: datetime
    #: Simulated seconds per real second.
    compression: float = DEFAULT_COMPRESSION
    _elapsed: timedelta = timedelta()

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise ValueError("SimulatedClock.start must be timezone-aware")
        if self.compression <= 0:
            raise ValueError(f"compression must be positive, got {self.compression}")

    def now(self) -> datetime:
        return self.start + self._elapsed

    @property
    def elapsed(self) -> timedelta:
        return self._elapsed

    def advance(self, seconds: float) -> datetime:
        """Move simulated time forward by simulated seconds."""
        if seconds < 0:
            # Time running backwards would corrupt every append-only
            # ledger the packs build. Backfill runs the clock forward
            # from an earlier start; it never rewinds.
            raise ValueError(f"cannot advance by a negative interval: {seconds}")
        self._elapsed += timedelta(seconds=seconds)
        return self.now()

    def advance_real(self, real_seconds: float) -> datetime:
        """Move forward by the simulated equivalent of real seconds.

        The run loop's entry point: it knows how long it actually
        slept, not how much simulated time that bought.
        """
        return self.advance(real_seconds * self.compression)

    def real_seconds_for(self, simulated_seconds: float) -> float:
        """How long a caller must really wait for a simulated span.

        The inverse of advance_real(), and the run loop's other half:
        it needs to know how long to sleep to make one simulated
        minute pass.
        """
        return simulated_seconds / self.compression


def day_fraction(moment: datetime) -> float:
    """Where in its day a moment falls, as 0.0 to 1.0.

    Lives here rather than in the arrival-rate code because it is a
    fact about a timestamp, not about arrivals, and the lifecycle code
    wants it too (shift boundaries, business hours). The diurnal
    intensity curves in scheduler.py are all functions of this.
    """
    seconds = moment.hour * 3600 + moment.minute * 60 + moment.second
    return seconds / 86400.0


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """A UTC datetime, without the tzinfo=UTC ceremony at every call.

    Exists because every pack's start date and every test's fixture
    needs one, and a naive datetime slipping in is a real failure mode
    that __post_init__ above is deliberately strict about.
    """
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): the clock deliberately does not sleep or own a
# thread. An earlier sketch had it drive its own loop, which meant tests had
# to either sleep for real or monkeypatch time.sleep. Moving the waiting out
# to the caller made the same clock serve both a fast test and a live run with
# no mode flag, and every test in tests/simulator/ now advances it directly.
#
# DEFERRED (known, intentional, not yet built): no pause/resume state lives
# here. The control server will own that, because "paused" is a property of
# the run loop, not of time -- a paused clock and a clock nobody is advancing
# are the same thing, and storing the flag twice is how the two drift.
#
# DEFERRED: no leap-second or DST handling. Everything is UTC by construction
# (__post_init__ rejects naive datetimes). If a pack ever needs local business
# hours in a real timezone -- the retail diurnal curve is a candidate -- the
# conversion belongs in that pack, not here, so that this clock keeps having
# exactly one notion of time.
