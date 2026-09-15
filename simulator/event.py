"""
event.py  (what happens, how often, and what it writes)

The piece that makes a world move rather than merely exist. An event
has a TRIGGER, which decides how many times it happens in an interval
and what each occurrence happens TO, and one or more EMISSIONS, which
decide what gets written.

TWO HIERARCHIES IN ONE MODULE, for now. The design calls for triggers
and emissions to be separate families, and they are -- separate base
classes, separate registries. They share a file while each has one
implementation, because two files with one class apiece is filing
rather than structure. The moment a second trigger kind exists (a
periodic one for end-of-day, a transition one for aviation's
milestones) this should split, and the split is a move rather than a
redesign.

SUBJECTS ARE ROWS, AND THAT IS WHAT MAKES EVENTS USEFUL WITHOUT A PICK
GENERATOR YET. `per: shop.products` means the event happens to each
product, and its emissions can refer to `subject.unit_price`. That is
enough to express the thing a sales event most needs -- a line whose
price is the product's price AT THAT MOMENT, copied rather than
linked, so it does not move when the product's price later does.

ARRIVALS ARE PER SUBJECT, NOT PER WORLD. Twelve products each selling
at half an hour's rate is not the same as the chain selling at six an
hour, because the per-subject form keeps its shape when the population
changes. A pack that doubles its product range should sell more, and
with a world-level rate it would not.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from simulator.context import EvaluationContext
from simulator.generators import Generator
from simulator.scheduler import FLAT, HourlyWeights, arrivals


class EventError(Exception):
    """An event could not be fired."""


# -- triggers ---------------------------------------------------------

class Trigger(ABC):
    """Decides how often an event happens, and to what."""

    name: ClassVar[str]

    @abstractmethod
    def occurrences(self, world: Any, elapsed_seconds: float) -> list[dict | None]:
        """One entry per occurrence in this interval.

        Each entry is the SUBJECT that occurrence happens to, or None
        for an event that happens to nothing in particular. Returning a
        list rather than a count is what lets a caller stay ignorant of
        whether an event is per-subject or not.
        """


@dataclass(frozen=True)
class RateTrigger(Trigger):
    """Poisson arrivals, optionally per row of a table.

    The curve is what stops the data reading as generated: a flat
    arrival rate means as many sales at 3am as at noon, which is the
    single clearest tell. See scheduler.py for why the sampling
    integrates a piecewise-constant intensity rather than thinning.
    """

    name: ClassVar[str] = "rate"

    rate_per_hour: float
    #: "silo.table", or None for an event with no subject.
    per: str | None = None
    curve: HourlyWeights = FLAT
    #: The random stream this event draws from. One per event, so
    #: adding a draw to one does not shift another.
    stream: str = "events"

    def occurrences(self, world: Any, elapsed_seconds: float) -> list[dict | None]:
        rng = world.rng.stream(self.stream)
        now = world.clock.now()
        if self.per is None:
            count = arrivals(rng, now, elapsed_seconds, self.rate_per_hour, self.curve)
            return [None] * count

        subjects = world.subject_rows(self.per)
        occurrences: list[dict | None] = []
        for subject in subjects:
            # Per subject, deliberately. A chain that doubles its
            # product range should sell more; a world-level rate would
            # keep the total flat and silently rescale each product.
            count = arrivals(rng, now, elapsed_seconds, self.rate_per_hour, self.curve)
            occurrences.extend([subject] * count)
        return occurrences


@dataclass(frozen=True)
class TransitionTrigger(Trigger):
    """Fires when an entity ENTERS a state.

    The counterpart to a rate: some things happen because time passed,
    and some happen because something changed. A flight leaving the
    gate is not a Poisson arrival -- it is what happens the moment that
    flight's state becomes `airborne`.

    THE SUBJECT IS THE TRANSITION, not a table row. It carries the
    entity's id under the name the persisted table calls it, so an
    update emission can address the row with
    `subject.work_order_id`, plus `state` and `previous_state` so a
    pack can record what it moved from.

    Reading the id back out of the database instead would be a query
    per transition to fetch a row the simulator already knows the key
    of.
    """

    name: ClassVar[str] = "transition"

    lifecycle: str
    entering: str

    def occurrences(self, world: Any, elapsed_seconds: float) -> list[dict | None]:
        return [
            transition for transition in world.transitions
            if transition["_lifecycle"] == self.lifecycle
            and transition["state"] == self.entering
        ]


# -- emissions --------------------------------------------------------

class Emission(ABC):
    """Decides what an occurrence writes."""

    name: ClassVar[str]

    @abstractmethod
    def emit(self, world: Any, context: EvaluationContext) -> int:
        """Write this emission's rows. Returns how many."""


@dataclass(frozen=True)
class InsertEmission(Emission):
    """New rows in one table, built column by column."""

    name: ClassVar[str] = "insert"

    silo: str
    table: str
    #: Column name -> generator, in DECLARED order. Order is the
    #: contract: each generated value goes into the context's row
    #: before the next generator runs, which is what lets a later
    #: column refer to an earlier one.
    columns: dict[str, Generator]
    #: How many rows one occurrence produces. A sale has one to four
    #: lines; a payment has exactly one.
    repeat_min: int = 1
    repeat_max: int = 1
    #: A lifecycle to start for each row written. The entity's id is
    #: the row's primary key, so the runner can find the row again when
    #: the entity moves.
    spawns: str | None = None
    #: The primary key column, needed to read that id back out of the
    #: row just built. Resolved at load from the schema rather than
    #: declared, so it cannot disagree with the table.
    key_column: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.silo}.{self.table}"

    def emit(self, world: Any, context: EvaluationContext) -> int:
        from simulator.relational import insert_rows

        count = (self.repeat_min if self.repeat_min == self.repeat_max
                 else context.rng.randint(self.repeat_min, self.repeat_max))
        rows = []
        for _ in range(count):
            for column_name, generator in self.columns.items():
                context.set_field(column_name, generator.value(context))
            # Finished under the QUALIFIED name, so a later emission in
            # the same event refers to it the way a pack writes it:
            # emitted.shop.sale_items.sum.line_total.
            row = context.finish_row(self.qualified)
            rows.append(row)
            if self.spawns is not None:
                assert self.key_column is not None  # the loader guarantees this
                world.spawn(self.spawns, row[self.key_column])
        if not rows:
            return 0
        table = world.pack.schemas[self.silo].table(self.table)
        return insert_rows(world.silo(self.silo), world.database(self.silo), table, rows)


@dataclass(frozen=True)
class UpdateEmission(Emission):
    """Revise an existing row rather than writing a new one.

    What the OOOI model needs: a flight leg is created when its
    schedule is published and then REVISED four to six times as it
    passes Gate Out, Wheels Off, Wheels On and Gate In. Modelling that
    as four separate rows would be a different thing wearing its name
    -- there is one flight, and what changes is what is known about it.
    """

    name: ClassVar[str] = "update"

    silo: str
    table: str
    #: Column name -> generator producing its new value.
    columns: dict[str, Generator]
    #: Column name -> generator producing the value to match on.
    where: dict[str, Generator]

    @property
    def qualified(self) -> str:
        return f"{self.silo}.{self.table}"

    def emit(self, world: Any, context: EvaluationContext) -> int:
        from simulator.relational import update_columns

        table = world.pack.schemas[self.silo].table(self.table)
        values = {}
        for column_name, generator in self.columns.items():
            value = generator.value(context)
            values[column_name] = value
            # Recorded on the context's row as it goes, so a later
            # column in the same update can refer to an earlier one --
            # the same contract an insert has.
            context.set_field(column_name, value)
        changed = update_columns(
            world.silo(self.silo), world.database(self.silo), table, values,
            {name: generator.value(context) for name, generator in self.where.items()},
        )
        # Cleared rather than finished: an update produces no row for
        # `emitted` to aggregate over, and leaving the half-built row
        # behind would leak into whatever ran next.
        context.row = {}
        return changed


# -- effects ----------------------------------------------------------

class Effect(ABC):
    """Changes something an occurrence did not itself write.

    The third family, and the one that makes a simulated business
    joined-up rather than a set of independent row factories. A sale
    that does not move stock is not a sale, it is a log entry.
    """

    name: ClassVar[str]

    @abstractmethod
    def apply(self, world: Any, context: EvaluationContext) -> int:
        """Apply the change. Returns rows affected."""


@dataclass(frozen=True)
class AdjustEffect(Effect):
    """Add to a numeric column on rows matching a key.

    EFFECTS RUN AFTER EMISSIONS, which is what makes the useful case
    expressible: the amount to deduct from stock is the quantity the
    lines just recorded, so `by` can be an expression over
    emitted.shop.sale_items.sum.quantity. Running them first would
    leave nothing to refer to.
    """

    name: ClassVar[str] = "adjust"

    silo: str
    table: str
    column: str
    #: Produces the amount to add. Negative for a deduction -- stated
    #: in the pack rather than implied by a separate `decrement` verb,
    #: so the sign is visible where it is written.
    by: Generator
    #: Column name -> generator producing the value to match on.
    where: dict[str, Generator]
    #: Optional lower bound. Stock cannot go negative.
    floor: Any | None = None

    @property
    def qualified(self) -> str:
        return f"{self.silo}.{self.table}.{self.column}"

    def apply(self, world: Any, context: EvaluationContext) -> int:
        from simulator.relational import adjust_column

        table = world.pack.schemas[self.silo].table(self.table)
        return adjust_column(
            world.silo(self.silo), world.database(self.silo), table, self.column,
            delta=self.by.value(context),
            where={name: generator.value(context) for name, generator in self.where.items()},
            floor=self.floor,
        )


# -- events -----------------------------------------------------------

@dataclass(frozen=True)
class Event:
    """One thing that happens, and what it writes when it does."""

    name: str
    trigger: Trigger
    #: In declared order, because a later emission may refer to what an
    #: earlier one wrote.
    emissions: tuple[Emission, ...] = field(default_factory=tuple)
    #: Applied after every emission, so they can refer to what was
    #: written.
    effects: tuple[Effect, ...] = field(default_factory=tuple)

    def fire(self, world: Any, elapsed_seconds: float) -> int:
        """Run every occurrence due in this interval. Returns rows written."""
        written = 0
        for subject in self.trigger.occurrences(world, elapsed_seconds):
            written += self._occur(world, subject)
        return written

    def _occur(self, world: Any, subject: dict | None) -> int:
        # ONE context per occurrence, not per emission. That is what
        # makes `emitted` mean "what this event has written so far"
        # rather than "what this emission wrote", which is the whole
        # point of being able to total a sale's lines onto the sale.
        context = world.context(f"event.{self.name}", subject=subject)
        written = 0
        for emission in self.emissions:
            try:
                written += emission.emit(world, context)
            except Exception as error:
                raise EventError(
                    f"event {self.name!r} failed while emitting to "
                    f"{getattr(emission, 'qualified', emission.name)}: {error}"
                ) from error
        for effect in self.effects:
            # After every emission, so an effect can refer to what they
            # wrote -- the stock to deduct is the quantity the lines
            # just recorded.
            try:
                effect.apply(world, context)
            except Exception as error:
                raise EventError(
                    f"event {self.name!r} failed applying {effect.name} to "
                    f"{getattr(effect, 'qualified', '')}: {error}"
                ) from error
        return written


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): arrivals are drawn PER SUBJECT rather than once
# for the world. Twelve products each selling at half an hour's rate is not the
# same as a chain selling at six an hour: only the per-subject form keeps its
# shape when the population changes, so a pack that doubles its product range
# sells more rather than silently rescaling each product.
#
# RESOLVED: one EvaluationContext per OCCURRENCE, not per emission. That is
# what makes `emitted` mean "what this event has written so far", which is
# what lets a sale total the lines it just wrote.
#
# RESOLVED: rows are finished under the QUALIFIED name (silo.table) so a pack
# refers to them the way it writes them elsewhere -- emitted.shop.sale_items
# rather than emitted.sale_items, which would be ambiguous the moment two silos
# have a table of the same name.
#
# RESOLVED: both hierarchies share this file while each has one implementation.
# Two files with one class apiece is filing rather than structure. The moment a
# periodic or transition trigger exists this should split, and the split is a
# move rather than a redesign.
#
# RESOLVED: an emission spawns an entity whose id IS the row's primary key,
# rather than a fresh number with a mapping alongside. A second mapping is a
# second thing that can fall out of step with the database, and there is
# nothing the fresh number would buy.
#
# DEFERRED, AND SHARPER THAN IT LOOKS: with inserts only, a pack cannot have
# BOTH a parent id on the children and an aggregate on the parent. A sale that
# totals its lines must be emitted after them, so its id does not exist while
# they are being built, and they cannot carry it. Found while writing the
# hardware-shop pack, where sale_items ended up with no sale_id. Two ways out,
# and the choice is a real design decision: an UpdateEmission that fills the
# total in afterwards, or an id generated once per OCCURRENCE and shared by
# every emission in it -- which is the smaller change and probably the right
# one, since a shared occurrence id is what a real system would have anyway.
#
# RESOLVED: effects exist, and run AFTER emissions. That order is what makes
# the useful case expressible at all -- the stock to deduct is the quantity the
# lines just recorded, so `by` can be an expression over an emitted aggregate.
# Running them first would leave nothing to refer to.
#
# RESOLVED: a deduction is a negative `by` rather than a separate `decrement`
# verb, so the sign is visible in the pack where it is written instead of being
# implied by which keyword was chosen.
#
# RESOLVED: TransitionTrigger and UpdateEmission arrived together, because
# neither is useful alone -- a transition with nothing to write changes no data,
# and an update with no trigger has no occasion to run. Together they are the
# OOOI model: one flight row revised as it passes each milestone.
#
# RESOLVED: an UpdateEmission clears the context row rather than finishing it.
# There is no row to aggregate over, and leaving a half-built one behind would
# leak into whatever ran next in the same occurrence.
#
# DEFERRED: no PeriodicTrigger. End-of-day roll-ups need one; it is small
# against this base class and should wait until a pack declares one.
#
# DEFERRED: Trigger and Emission take `world: Any` rather than a World, purely
# to avoid an import cycle -- World holds a PackSpec, which will hold Events.
# Worth fixing with a protocol once the shape settles; typing it as Any means
# mypy cannot check these call sites, which is a real loss.
