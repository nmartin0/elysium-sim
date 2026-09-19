"""
oracle.py  (what was true, and when -- independently of any consumer)

The problem this solves. Every drift operation except one eventually
throws something somewhere, so a test can assert that a consumer
failed well. RescaleColumn throws nothing: every read succeeds, every
type still checks, and the answers are silently a hundred times
bigger. Its pass condition is not "it failed well" but "the numbers
are still right" -- and nothing could say what right was, because the
database is the only record and the database is what moved.

So the oracle is a second record. It samples declared aggregates at
points in simulated time and keeps the series, which makes three
questions answerable that were not:

  - what was the total on day five?
  - did it change at a moment no event explains?
  - does a consumer's cached answer still match the world it came
    from?

A TIME series rather than write interception. The alternative was to
observe every insert, update and adjustment as it happened and
maintain running totals. That means threading the oracle through every
write path, and maintaining an incremental sum correctly through
updates and rescales is its own small pile of arithmetic to get wrong
-- which would leave the referee needing a referee. Sampling asks the
database the same question a consumer would, and the whole point is to
be a record of what a consumer would have seen.

It samples, so it cannot see between samples. A change made and undone
inside one interval is invisible. That is a real limit and stated
rather than hidden; the sampling cadence is the resolution, and the
runner samples every tick by default.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from simulator.worldview import WorldView

#: The aggregate functions a watch may use. Closed, like every other
#: vocabulary here, and deliberately the same four the reference
#: language offers over emitted rows.
AGGREGATES = frozenset({"sum", "count", "min", "max"})


def fetch_all(*args, **kwargs):
    """Indirection so a test can make a read fail for a reason that is
    NOT the database's -- which is the case the narrowed except exists
    to let through, and which had no test until a control ran against
    it and stayed silent."""
    from simulator.relational import fetch_all as _fetch_all

    return _fetch_all(*args, **kwargs)


class OracleError(Exception):
    """A watch was malformed, or could not be sampled."""


@dataclass(frozen=True)
class Watch:
    """One number the oracle keeps an eye on."""

    silo: str
    table: str
    column: str
    aggregate: str = "sum"

    def __post_init__(self) -> None:
        if self.aggregate not in AGGREGATES:
            raise OracleError(
                f"{self.aggregate!r} is not an aggregate; available: {sorted(AGGREGATES)}"
            )

    @property
    def name(self) -> str:
        return f"{self.aggregate}({self.silo}.{self.table}.{self.column})"


@dataclass(frozen=True)
class Sample:
    """One watch's value at one moment of simulated time.

    `ok` distinguishes two things that both look like no value, and
    conflating them was a real bug: sum over an empty table returns
    NULL, so a watch on a business that has not traded yet reported
    None for exactly the same reason a watch on a dropped column does.
    went_blind() then fired on every world, at its first tick.
    """

    at: datetime
    value: Any
    #: Whether the query ran at all. False means the column or table is
    #: no longer there; a None value with ok=True means the table is
    #: simply empty.
    ok: bool = True


@dataclass
class Oracle:
    """What each watched number was, at each moment it was sampled."""

    watches: tuple[Watch, ...] = ()
    series: dict[str, list[Sample]] = field(default_factory=dict)

    def sample(self, world: WorldView) -> None:
        """Record every watch's value at the world's current moment.

        Failures are recorded as None rather than raised. A watch whose
        column has been dropped is exactly the situation the oracle
        exists to observe, and refusing to sample would turn the
        instrument off at the moment it became interesting.
        """
        from simulator.dialect import dialect_for

        now = world.clock.now()
        for watch in self.watches:
            # Outside the try, deliberately. A watch naming a silo that
            # does not exist is a mistake in whoever declared it, not a
            # silo that has gone away, and it should say so rather than
            # be recorded as drift.
            silo = world.silo(watch.silo)
            dialect = dialect_for(silo.kind)
            try:
                rows = fetch_all(
                    silo, world.database(watch.silo),
                    f"SELECT {watch.aggregate}({dialect.quote(watch.column)}) "
                    f"FROM {dialect.quote(watch.table)}",
                )
                value, ok = (rows[0][0] if rows else None), True
            except silo.driver_errors():
                # The column or table is no longer there, which is
                # exactly what this instrument exists to observe. A
                # KeyError or a TypeError would reach this line too
                # before it was narrowed, and read as drift -- a wrong
                # answer nobody investigates, rather than a crash
                # somebody fixes.
                value, ok = None, False
            self.series.setdefault(watch.name, []).append(
                Sample(at=now, value=value, ok=ok))

    # -- asking it things --------------------------------------------

    def at(self, watch: Watch, moment: datetime) -> Any:
        """The value as of a moment: the last sample at or before it.

        "As of" rather than "at", because a consumer that read the
        database at 14:07 saw whatever was true then, and the nearest
        sample before that is the best account of it the oracle has.
        """
        samples = self.series.get(watch.name, [])
        answer = None
        for sample in samples:
            if sample.at <= moment:
                answer = sample.value
            else:
                break
        return answer

    def latest(self, watch: Watch) -> Any:
        samples = self.series.get(watch.name, [])
        return samples[-1].value if samples else None

    def jumps(self, watch: Watch, factor: Decimal) -> list[Sample]:
        """Samples where the value multiplied by roughly `factor`.

        What makes a silent rescale visible. A hundredfold step between
        two consecutive samples is not something a business does; it is
        something a migration does. Comparing ratios rather than
        differences is what lets one threshold work whether the column
        holds tens or millions.

        Tolerant by a tenth, because between the two samples the
        simulation also wrote new rows at the old scale -- so the step
        is never exactly the factor, which is itself the mark of the
        botched migration rather than a clean one.
        """
        found = []
        previous = None
        for sample in self.series.get(watch.name, []):
            if (previous is not None and previous not in (None, 0)
                    and sample.value is not None):
                ratio = Decimal(str(sample.value)) / Decimal(str(previous))
                if abs(ratio - factor) <= abs(factor) / 10:
                    found.append(sample)
            previous = sample.value
        return found

    def went_blind(self, watch: Watch) -> datetime | None:
        """When a watch stopped being answerable, if it did.

        A column that was there and is not is the destructive drift a
        consumer has to notice. The oracle noticing is not the same as
        the consumer noticing -- it is the thing a test compares the
        consumer against.
        """
        for sample in self.series.get(watch.name, []):
            if not sample.ok:
                return sample.at
        return None


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: sampling rather than intercepting writes. Maintaining running totals
# through inserts, updates, adjustments and rescales is its own pile of
# arithmetic to get wrong, which would leave the referee needing a referee.
# Sampling asks the database the same question a consumer would, which is
# exactly what a record of "what a consumer would have seen" should do.
#
# RESOLVED: Sample carries `ok` as well as a value. Without it, sum over an
# empty table -- which returns NULL -- was indistinguishable from a sample that
# could not run, so went_blind() fired on every world at its first tick, before
# the business had traded. Found by the test asserting a healthy watch never
# goes blind.
#
# RESOLVED: the except catches the silo's driver errors rather than Exception.
# A mistyped watch, or a bug in this file, used to be recorded as the watch
# going blind -- reporting drift that had not happened, which is worse than
# crashing because nobody investigates a wrong answer.
#
# RESOLVED: a failed sample is recorded rather than raised. A watch
# whose column has been dropped is the situation the oracle exists to observe;
# refusing to sample would turn the instrument off at the moment it became
# interesting.
#
# LIMIT, stated rather than hidden: the oracle cannot see between samples. A
# change made and undone inside one interval is invisible to it. The sampling
# cadence is the resolution.
#
# RESOLVED: watches are declared in the PACK, which means the person running a
# training world can ask for them. See spec/watches.py. They remain constructible
# in Python, because a test wanting one number checked should not have to write
# a pack file.
#
# SUPERSEDED: watches are declared in Python
# rather than in a pack file. They belong in the pack -- a business knows which
# of its numbers matter -- and the declaration is a small addition once the
# shape has been used in anger. Left out on purpose so the vocabulary is drawn
# from real use rather than guessed at, which is how every other part of the
# pack vocabulary was arrived at.
#
# DEFERRED: no per-row oracle, so "which invoice changed" cannot be answered,
# only "the total moved". Row-level truth means keeping a copy of every row
# written, which is a different and much heavier instrument; the aggregate one
# answers the question class-D drift actually poses.
