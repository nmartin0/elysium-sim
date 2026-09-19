"""
event.py  (what happens, how often, and what it writes)

The piece that makes a world move rather than merely exist. An event
has a trigger, which decides how many times it happens in an interval
and what each occurrence happens to, and one or more emissions, which
decide what gets written.

Two hierarchies in one module, for now. The design calls for triggers
and emissions to be separate families, and they are -- separate base
classes, separate registries. They share a file while each has one
implementation, because two files with one class apiece is filing
rather than structure. The moment a second trigger kind exists (a
periodic one for end-of-day, a transition one for aviation's
milestones) this should split, and the split is a move rather than a
redesign.

Subjects are rows, and that is what makes events useful without a pick
generator yet. `per: shop.products` means the event happens to each
product, and its emissions can refer to `subject.unit_price`. That is
enough to express the thing a sales event most needs -- a line whose
price is the product's price at that moment, copied rather than
linked, so it does not move when the product's price later does.

Arrivals are per subject, not per world. Twelve products each selling
at half an hour's rate is not the same as the chain selling at six an
hour, because the per-subject form keeps its shape when the population
changes. A pack that doubles its product range should sell more, and
with a world-level rate it would not.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar, cast

from simulator.context import EvaluationContext
from simulator.generators import Generator
from simulator.scheduler import FLAT, HourlyWeights, arrivals
from simulator.worldview import ServesCollections, WorldView, WritesFiles


class EventError(Exception):
    """An event could not be fired."""


# -- triggers ---------------------------------------------------------

class Trigger(ABC):
    """Decides how often an event happens, and to what."""

    name: ClassVar[str]

    @abstractmethod
    def occurrences(self, world: WorldView, elapsed_seconds: float) -> list[dict | None]:
        """One entry per occurrence in this interval.

        Each entry is the subject that occurrence happens to, or None
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

    def occurrences(self, world: WorldView, elapsed_seconds: float) -> list[dict | None]:
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
    """Fires when an entity enters a state.

    The counterpart to a rate: some things happen because time passed,
    and some happen because something changed. A flight leaving the
    gate is not a Poisson arrival -- it is what happens the moment that
    flight's state becomes `airborne`.

    The subject is the transition, not a table row. It carries the
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

    def occurrences(self, world: WorldView, elapsed_seconds: float) -> list[dict | None]:
        return [
            transition for transition in world.transitions
            if transition["_lifecycle"] == self.lifecycle
            and transition["state"] == self.entering
        ]


@dataclass(frozen=True)
class PeriodicTrigger(Trigger):
    """Fires once every fixed interval of simulated time.

    Nightly exports, fortnightly payroll, month-end close: things that
    happen because the calendar said so rather than because anything
    changed or because a rate came up.

    Stateless, BY counting boundary crossings. It fires once per
    interval boundary crossed during the tick, computed from the clock
    rather than from a remembered last-fired time. That makes it
    tick-size independent for free -- one hourly event fires 24 times
    whether the day is run in 24 ticks or 2 -- and leaves nothing to
    restore when a run is resumed.
    """

    name: ClassVar[str] = "periodic"

    every_seconds: float

    def occurrences(self, world: WorldView, elapsed_seconds: float) -> list[dict | None]:
        if elapsed_seconds <= 0:
            return []
        now = world.clock.now()
        started = now.timestamp() - elapsed_seconds
        first = int(started // self.every_seconds) + 1
        last = int(now.timestamp() // self.every_seconds)
        # Each occurrence carries the boundary it crossed, not the end
        # of the tick. Otherwise a tick longer than the interval fires
        # the right number of times with every one of them stamped
        # identically -- three nightly exports that all believe they
        # are for the same day, writing the same filename over each
        # other. Found by a test expecting three files and getting one.
        return [
            {"_at": datetime.fromtimestamp(index * self.every_seconds, tz=now.tzinfo)}
            for index in range(first, last + 1)
        ]


# -- emissions --------------------------------------------------------

class Emission(ABC):
    """Decides what an occurrence writes."""

    name: ClassVar[str]

    @abstractmethod
    def emit(self, world: WorldView, context: EvaluationContext) -> int:
        """Write this emission's rows. Returns how many."""


@dataclass(frozen=True)
class InsertEmission(Emission):
    """New rows in one table, built column by column."""

    name: ClassVar[str] = "insert"

    silo: str
    table: str
    #: Column name -> generator, in declared order. Order is the
    #: contract: each generated value goes into the context's row
    #: before the next generator runs, which is what lets a later
    #: column refer to an earlier one.
    columns: dict[str, Generator]
    #: How many rows one occurrence produces. A sale has one to four
    #: lines; a payment has exactly one.
    repeat_min: int = 1
    repeat_max: int = 1
    #: Tables to choose a row from before each row is built, keyed by
    #: the name a pack refers to them by. A sale line picks a product;
    #: a work order picks a technician.
    picks: dict[str, str] = field(default_factory=dict)
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

    def emit(self, world: WorldView, context: EvaluationContext) -> int:
        from simulator.relational import insert_rows

        count = (self.repeat_min if self.repeat_min == self.repeat_max
                 else context.rng.randint(self.repeat_min, self.repeat_max))
        rows = []
        for _ in range(count):
            # Per row, not per occurrence. A sale with three lines is
            # three different products; picking once for the whole
            # occurrence would make every line of every sale the same
            # item, which looks like data and is not.
            for name, qualified in self.picks.items():
                candidates = world.subject_rows(qualified)
                if not candidates:
                    raise EventError(
                        f"cannot pick from {qualified!r}: it has no rows. Reference "
                        f"data has to be seeded before anything can choose from it."
                    )
                context.picked[name] = context.rng.choice(candidates)
            for column_name, generator in self.columns.items():
                context.set_field(column_name, generator.value(context))
            # Finished under the qualified name, so a later emission in
            # the same event refers to it the way a pack writes it:
            # emitted.shop.sale_items.sum.line_total.
            row = context.finish_row(self.qualified)
            rows.append(row)
            if self.spawns is not None:
                assert self.key_column is not None  # the loader guarantees this
                world.spawn(self.spawns, row[self.key_column])
        if not rows:
            return 0
        table = world.schema(self.silo).table(self.table)
        return insert_rows(world.silo(self.silo), world.database(self.silo), table, rows)


def _pick_fresh(world: WorldView, qualified: str,
                context: EvaluationContext) -> dict:
    """One row, read now rather than from the cache.

    WHY NOT world.subject_rows(). That cache is right for reference
    data -- products, branches, the things seeded once and read forever
    -- and wrong here, because an update picks from a table the
    SIMULATION IS WRITING TO. An invoice raised this morning is not in
    a list read at startup, so a cached pick would choose only from the
    rows that existed before anything happened, and the trap it exists
    to avoid would come back wearing a different hat.

    The cost is a query per occurrence, which is why picking on an
    update is opt-in rather than something every update does.
    """
    from simulator.dialect import dialect_for
    from simulator.relational import fetch_all

    silo_name, table_name = qualified.split(".")
    silo = world.silo(silo_name)
    schema = world.schema(silo_name)
    table = schema.table(table_name)
    dialect = dialect_for(silo.kind)
    names = [column.name for column in table.columns]
    selected = ", ".join(dialect.quote(name) for name in names)
    rows = fetch_all(silo, world.database(silo_name),
                     f"SELECT {selected} FROM {dialect.quote(table_name)}")
    if not rows:
        raise EventError(
            f"cannot pick from {qualified!r}, which has no rows yet. An update that "
            f"picks runs only once something has been written to pick from."
        )
    return dict(zip(names, context.rng.choice(rows), strict=True))


@dataclass(frozen=True)
class UpdateEmission(Emission):
    """Revise an existing row rather than writing a new one.

    What the OOOI model needs: a flight leg is created when its
    schedule is published and then revised four to six times as it
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
    #: Tables to choose a row from before the update is built, so the
    #: `where` can name ONE row. See _pick_fresh for why these are read
    #: fresh rather than from the subject cache.
    picks: dict[str, str] = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        return f"{self.silo}.{self.table}"

    def emit(self, world: WorldView, context: EvaluationContext) -> int:
        from simulator.relational import update_columns

        table = world.schema(self.silo).table(self.table)
        for name, qualified in self.picks.items():
            context.picked[name] = _pick_fresh(world, qualified, context)
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


@dataclass(frozen=True)
class Window:
    """How far back an export reaches.

    WHY A WINDOW AND NOT A WATERMARK. The obvious design is to remember
    what was last exported and send everything since -- and that needs
    somewhere to remember it, which is state the simulator would have
    to keep in step with databases anything else can write to.

    A window needs nothing remembered. "Last week's transactions" is a
    function of the clock and a declared span, which is also what a
    real weekly export IS: nobody computes a watermark, they run the
    report for the period. Two exports that overlap, or a run that
    skips a week, behave the way the real thing would rather than the
    way a bookmark would.

    Measured before this existed: a simulated year wrote 925,000 rows,
    most of them an export rewriting its whole source table from the
    beginning every time it fired.
    """

    column: str
    seconds: float

    def clause(self, dialect: Any, placeholder: str) -> str:
        return f" WHERE {dialect.quote(self.column)} >= {placeholder}"

    def earliest(self, now: datetime) -> datetime:
        return now - timedelta(seconds=self.seconds)


@dataclass(frozen=True)
class PublishEmission(Emission):
    """Write a file into a file-drop silo.

    The integration small businesses actually have. A bank statement,
    a supplier price list, a payroll file: not a database connection, a
    folder somebody drops a CSV into. The rows come from a relational
    silo, because that is what a real export job does -- it queries the
    operational system and writes what it finds.

    Published atomically by the silo, so a consumer polling the folder
    sees the file either absent or complete. See filedrop.py for why
    that matters more than it sounds.
    """

    name: ClassVar[str] = "publish"

    #: The file-drop silo to write into.
    silo: str
    #: Produces the file name. Usually a template with a date in it.
    filename: Generator
    #: Where the rows come from: a relational silo and table.
    source_silo: str
    source_table: str
    #: Which columns to include, in order. Declared rather than "all",
    #: because an export is a contract with whoever reads it and should
    #: not silently gain a column when the table does.
    columns: tuple[str, ...]
    #: How far back to reach, or None for everything. See Window.
    window: "Window | None" = None

    #: What the filename template may refer to. A file named after the
    #: day it covers is how these exports are really named, and the
    #: name has to come from somewhere -- but a template resolves
    #: references, not generators, so these are offered as fields of
    #: the row being built rather than as a second mechanism beside the
    #: reference language.
    FACTS: ClassVar[tuple[str, ...]] = ("today", "now")

    @property
    def qualified(self) -> str:
        return f"{self.silo}(file)"

    def emit(self, world: WorldView, context: EvaluationContext) -> int:
        from simulator.dialect import dialect_for
        from simulator.relational import fetch_all

        context.set_field("today", context.now.date().isoformat())
        context.set_field("now", context.now.isoformat())
        source = world.silo(self.source_silo)
        dialect = dialect_for(source.kind)
        selected = ", ".join(dialect.quote(name) for name in self.columns)
        statement = f"SELECT {selected} FROM {dialect.quote(self.source_table)}"
        parameters: tuple = ()
        if self.window is not None:
            statement += self.window.clause(dialect, dialect.placeholder)
            parameters = (self.window.earliest(context.now),)
        rows = fetch_all(source, world.database(self.source_silo), statement, parameters)
        name = str(self.filename.value(context))
        # Cleared for the same reason an update clears: the facts above
        # are this emission's own, and leaving them on the row would
        # carry them into whatever runs next in the occurrence.
        context.row = {}
        # Narrowed rather than asserted: the loader refuses a
        # publication whose silo is not a filedrop, so by the time this
        # runs the method is there. See worldview.py.
        destination = cast("WritesFiles", world.silo(self.silo))
        destination.write_csv(name, self.columns, rows)
        return len(rows)


@dataclass(frozen=True)
class ExposeEmission(Emission):
    """Publish a collection through a REST silo.

    The other half of how a small business is reached. A file drop is
    a folder somebody writes into; an API is a collection somebody
    polls, and the difference is not cosmetic -- a file is a document
    with a name and a moment, a collection is the current state of
    something.

    So it replaces rather than appends. Asking an API for invoices
    returns the invoices, not a new batch each time. A pack wanting an
    append-only feed is describing events rather than a collection, and
    should say so with a different word once one exists.

    JSON has no DECIMAL and no DATE. Both have to be converted, and how
    is a real decision rather than a detail: money becomes a string,
    not a float, because a float cannot represent 0.10 and would
    reintroduce one layer up exactly the error the schema layer refuses
    to allow in a column. Real APIs agree -- they send money as a
    string or as integer minor units, never as a JSON number.
    Timestamps become ISO 8601 strings, which is what these APIs emit
    and what makes date handling a consumer's problem to get right.
    """

    name: ClassVar[str] = "expose"

    #: The REST silo to publish through.
    silo: str
    #: The collection name, which becomes the path: /v1/<collection>.
    collection: str
    source_silo: str
    source_table: str
    columns: tuple[str, ...]
    window: "Window | None" = None

    @property
    def qualified(self) -> str:
        return f"{self.silo}({self.collection})"

    def emit(self, world: WorldView, context: EvaluationContext) -> int:
        from simulator.dialect import dialect_for
        from simulator.relational import fetch_all

        source = world.silo(self.source_silo)
        dialect = dialect_for(source.kind)
        selected = ", ".join(dialect.quote(name) for name in self.columns)
        statement = f"SELECT {selected} FROM {dialect.quote(self.source_table)}"
        parameters: tuple = ()
        if self.window is not None:
            statement += self.window.clause(dialect, dialect.placeholder)
            parameters = (self.window.earliest(context.now),)
        rows = fetch_all(source, world.database(self.source_silo), statement, parameters)
        records = [
            {name: _json_safe(value) for name, value in zip(self.columns, row, strict=True)}
            for row in rows
        ]
        destination = cast("ServesCollections", world.silo(self.silo))
        destination.publish(self.collection, records)
        return len(records)


def _json_safe(value: Any) -> Any:
    """Convert a database value to something JSON can carry.

    See ExposeEmission's note on why money becomes a string rather than
    a number. Anything JSON already handles passes through untouched,
    so a null stays null rather than becoming "None".
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if value is None or isinstance(value, str | bool | int | float):
        return value
    return str(value)


# -- effects ----------------------------------------------------------

class Effect(ABC):
    """Changes something an occurrence did not itself write.

    The third family, and the one that makes a simulated business
    joined-up rather than a set of independent row factories. A sale
    that does not move stock is not a sale, it is a log entry.
    """

    name: ClassVar[str]

    @abstractmethod
    def apply(self, world: WorldView, context: EvaluationContext) -> int:
        """Apply the change. Returns rows affected."""


@dataclass(frozen=True)
class AdjustEffect(Effect):
    """Add to a numeric column on rows matching a key.

    Effects run after emissions, which is what makes the useful case
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

    def apply(self, world: WorldView, context: EvaluationContext) -> int:
        from simulator.relational import adjust_column

        table = world.schema(self.silo).table(self.table)
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

    def fire(self, world: WorldView, elapsed_seconds: float) -> int:
        """Run every occurrence due in this interval. Returns rows written."""
        written = 0
        for subject in self.trigger.occurrences(world, elapsed_seconds):
            written += self._occur(world, subject)
        return written

    def _occur(self, world: WorldView, subject: dict | None) -> int:
        # A trigger may say when its occurrence happened, which matters
        # when one tick contains several -- see PeriodicTrigger. The key
        # is prefixed so it cannot collide with a column a pack might
        # legitimately reference.
        at = None
        if subject is not None and "_at" in subject:
            subject = dict(subject)
            at = subject.pop("_at")
        # One context per occurrence, not per emission. That is what
        # makes `emitted` mean "what this event has written so far"
        # rather than "what this emission wrote", which is the whole
        # point of being able to total a sale's lines onto the sale.
        context = world.context(f"event.{self.name}", subject=subject, now=at)
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
# RESOLVED (kept for history): arrivals are drawn per subject rather than once
# for the world. Twelve products each selling at half an hour's rate is not the
# same as a chain selling at six an hour: only the per-subject form keeps its
# shape when the population changes, so a pack that doubles its product range
# sells more rather than silently rescaling each product.
#
# RESOLVED: one EvaluationContext per occurrence, not per emission. That is
# what makes `emitted` mean "what this event has written so far", which is
# what lets a sale total the lines it just wrote.
#
# RESOLVED: rows are finished under the qualified name (silo.table) so a pack
# refers to them the way it writes them elsewhere -- emitted.shop.sale_items
# rather than emitted.sale_items, which would be ambiguous the moment two silos
# have a table of the same name.
#
# RESOLVED: both hierarchies share this file while each has one implementation.
# Two files with one class apiece is filing rather than structure. The moment a
# periodic or transition trigger exists this should split, and the split is a
# move rather than a redesign.
#
# RESOLVED: an emission spawns an entity whose id is the row's primary key,
# rather than a fresh number with a mapping alongside. A second mapping is a
# second thing that can fall out of step with the database, and there is
# nothing the fresh number would buy.
#
# DEFERRED, and sharper than it looks: with inserts only, a pack cannot have
# both a parent id on the children and an aggregate on the parent. A sale that
# totals its lines must be emitted after them, so its id does not exist while
# they are being built, and they cannot carry it. Found while writing the
# hardware-shop pack, where sale_items ended up with no sale_id. Two ways out,
# and the choice is a real design decision: an UpdateEmission that fills the
# total in afterwards, or an id generated once per occurrence and shared by
# every emission in it -- which is the smaller change and probably the right
# one, since a shared occurrence id is what a real system would have anyway.
#
# RESOLVED: effects exist, and run after emissions. That order is what makes
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
# RESOLVED: each periodic occurrence carries the boundary it crossed rather than
# the end of the tick. A tick longer than the interval otherwise fires the right
# number of times with every occurrence stamped identically -- three nightly
# exports all believing they are for the same day, writing the same filename
# over each other. Found by a test expecting three files and getting one.
#
# RESOLVED: PeriodicTrigger counts interval boundaries crossed rather than
# remembering when it last fired. That makes it tick-size independent without
# trying -- an hourly event fires 24 times whether the day is run in 24 ticks
# or 2 -- and leaves nothing to restore when a run is resumed.
#
# DEFERRED: a published file always contains the whole source table. Real
# exports are usually incremental -- yesterday's transactions, not every
# transaction ever -- and a pack running for a simulated year would write a file
# that grows without bound. Doing it properly needs a way to say "rows since the
# last publication", which is a declaration shape worth drawing from a pack that
# wants one rather than guessing. Full exports are themselves real: a price
# list or a customer list is sent whole.
#
# RESOLVED: ExposeEmission is its own emission rather than a flag on publish.
# A file is a document with a name and a moment; a collection is the current
# state of something. Sharing one declaration would have needed a filename that
# means nothing for an API and a collection that means nothing for a folder.
#
# DEFERRED: a collection is always the whole source table, and is replaced on
# every publication. Real APIs paginate over a collection that grows, which the
# REST silo already serves correctly -- what is missing is a pack's way to say
# "append these" rather than "this is now the set", and that is a description of
# events rather than of a collection.
#
# DEFERRED: Trigger and Emission take `world: WorldView` rather than a World, purely
# to avoid an import cycle -- World holds a PackSpec, which will hold Events.
# Worth fixing with a protocol once the shape settles; typing it as Any means
# mypy cannot check these call sites, which is a real loss.
