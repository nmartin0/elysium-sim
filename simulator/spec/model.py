"""
model.py  (a pack file, parsed -- data, and nothing else)

Frozen dataclasses. Nothing here reads a file, validates a
cross-reference, or runs anything; loader.py does all three. The split
is the same one schema.py and dialect.py draw, and for the same
reason: a parsed pack should be inspectable and comparable without
dragging in the machinery that produced it.

Why these are not one base class. They share no behaviour. A SiloSpec
and a LifecycleSpec have nothing in common except having been read
from the same file, and a `Spec` parent would carry nothing but its
own name. This is the counter-example to the generator hierarchy in
the same project: there, a real contract with unrelated
implementations; here, unrelated data with no contract.

What is deliberately absent: events, triggers, emissions and effects.
They are the largest part of the eventual vocabulary and none of them
can be written yet, because they all need the world layer to say what
an event happens to. Declaring their shape now would be guessing, and
a spec model is exactly the wrong place to guess -- every pack file
written against it would have to change.
"""

from dataclasses import dataclass, field

from simulator.drift import SchemaChange
from simulator.event import Event
from simulator.lifecycle import Lifecycle
from simulator.oracle import Watch
from simulator.schema import Schema

#: Hourly weights for an arrival curve, after validation.
Curve = tuple[float, ...]


@dataclass(frozen=True)
class SiloSpec:
    """One system the simulated business runs."""

    #: The business's name for it -- "dispatch", "books" -- not a
    #: technology name. Two silos can share a technology and still be
    #: different systems.
    name: str
    #: A key in simulator.silos.SILO_TYPES.
    kind: str
    #: The database to create inside it, for relational kinds. None for
    #: file and API silos, which have no such concept.
    database: str | None = None
    #: Kind-specific settings passed through to the silo constructor.
    options: dict = field(default_factory=dict)
    #: Tables the consumer accounts may NOT read. A real deployment
    #: rarely grants a reporting tool everything -- payroll and audit
    #: tables are the usual exceptions -- and a consumer meeting a
    #: table it can find but cannot select from is a genuine production
    #: failure worth being able to reproduce. Empty means everything is
    #: readable, which is what every pack said before this existed.
    withheld: tuple[str, ...] = ()
    #: The silo this one is a reporting copy of, refreshed on a
    #: schedule. A reporting tool is usually pointed at one of these
    #: rather than at the system of record, so "the number was right
    #: five minutes ago" is a real support call and one nothing here
    #: could produce.
    replicates: str | None = None
    #: How often the copy is refreshed. Between refreshes the replica
    #: is behind by up to this much, which IS the lag -- there is no
    #: separate delay to configure, because a materialised copy is
    #: exactly as stale as the time since it was last rebuilt.
    refresh_seconds: float = 0.0


@dataclass(frozen=True)
class SeedStep:
    """Reference data to write once, before any simulation runs.

    Stores, products, departments, aircraft: the slow-changing things
    that exist before any activity happens.
    """

    silo: str
    table: str
    #: How many rows to write. Standing alone, that is the whole step;
    #: with `per`, it is how many rows Per subject -- which is what a
    #: join table needs, since a technician has several skills and not
    #: one.
    count: int
    #: "silo.table" to seed one row per row of another table, instead of
    #: a fixed count. Without this a pack cannot key one reference table
    #: to another -- seeding inventory for the products it just seeded
    #: was impossible, because the two steps share an id counter and
    #: produced different skus.
    per: str | None
    #: Tables to choose a row from before each row is built, keyed by
    #: the name a pack refers to them by. The same shape an emission
    #: uses, and here for the same reason: a join row needs one end
    #: picked from somewhere, and a generator returning a single value
    #: cannot keep two columns agreeing about which row it chose.
    picks: dict[str, str]
    #: Whether a subject's rows may pick the same row twice. Off by
    #: default, because repeated picks are right for some things -- a
    #: customer buying the same item on two occasions -- and wrong for
    #: a join table, where "two skills each" means two DIFFERENT
    #: skills and a repeat is a defect.
    distinct_picks: bool
    #: Rows written out literally, instead of generated. A lookup table
    #: -- skills, branches, statuses, categories -- wants ITS rows, one
    #: each, and no generator can say that: `choice` draws with
    #: replacement, so six draws from six options gave two skills named
    #: the same and none named several of the others. Empty for a step
    #: that generates.
    rows: tuple[dict, ...]
    #: Column name -> generator declaration, already validated but not
    #: yet built. Built by the runner, which owns the context they need.
    columns: dict[str, dict]

    @property
    def qualified(self) -> str:
        return f"{self.silo}.{self.table}"


@dataclass(frozen=True)
class LifecyclePersistence:
    """Where a lifecycle's entities live in the database.

    A lifecycle is otherwise internal to the simulator: entities walk
    their states in memory and nothing outside can see it. Declaring
    where the state is written is what makes the progression visible to
    a consumer -- a work order whose `status` column really does move
    from quoted to approved to completed.

    The id column is not declared. It is the table's primary key, which
    the schema already states; asking a pack to repeat it would be a
    second place for the two to disagree.
    """

    silo: str
    table: str
    id_column: str
    state_column: str
    #: Where the moment a state was entered is written, if anywhere.
    #: Optional because most tables do not have such a column, and
    #: required for a world that will be RESUMED: without it a resume
    #: knows what state each entity is in and not how long it has been
    #: there, so every dwell restarts and a year built from twelve legs
    #: restarts every entity's clock twelve times.
    entered_column: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.silo}.{self.table}"


@dataclass(frozen=True)
class Migration:
    """One schema change, due at a point in simulated time.

    `at` is measured from the world's start rather than from a date, so
    a pack describes its own history ("on day forty") rather than
    depending on when a run happens to begin.
    """

    silo: str
    at_seconds: float
    change: SchemaChange


@dataclass(frozen=True)
class PackSpec:
    """One whole simulated organization, as declared."""

    name: str
    description: str
    silos: dict[str, SiloSpec]
    #: Silo name -> the schema it holds. Only relational silos appear.
    schemas: dict[str, Schema]
    curves: dict[str, Curve]
    lifecycles: dict[str, Lifecycle]
    #: Lifecycle name -> where its state is written. Absent for
    #: lifecycles that stay internal, which is legitimate: a pack may
    #: use a state machine to drive behaviour without the business
    #: system having a column for it.
    persistence: dict[str, LifecyclePersistence]
    seed: tuple[SeedStep, ...]
    #: In declared order, though nothing depends on the order between
    #: events -- only on the order of emissions within one.
    events: tuple[Event, ...] = ()
    #: In the order they are due. Validated at load by applying them
    #: in sequence to the declared schema, so a pack that drops a
    #: column twice fails when the file is read.
    migrations: tuple[Migration, ...] = ()
    #: Numbers the pack considers worth keeping an independent record
    #: of. Empty by default: the oracle costs a query per watch per
    #: tick, and a world nobody is checking should not pay for it.
    watches: tuple[Watch, ...] = ()

    def silo(self, name: str) -> SiloSpec:
        if name not in self.silos:
            raise KeyError(f"no silo {name!r} in pack {self.name!r}; it has {sorted(self.silos)}")
        return self.silos[name]


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): there is no common Spec base class. These types
# share no behaviour -- only an origin -- and a parent would carry nothing but
# its own name. Worth stating because the same project has a large generator
# hierarchy: the difference is that generators share a real contract with
# unrelated implementations, and these are unrelated data with no contract.
#
# RESOLVED: SeedStep keeps its column declarations as raw dicts rather than
# built Generator objects. A generator needs an EvaluationContext to produce
# anything, and the context belongs to whoever is running the simulation, not
# to the parsed file. The declarations are fully validated at load; only the
# construction is deferred.
#
# RESOLVED (kept for history): events are here now, and were deliberately held
# back until the world layer existed to say what an event happens to. The shape
# they took -- a subject being a row of a declared table -- is drawn from that,
# and would have been guessed wrong beforehand.
#
# DEFERRED (known, intentional, not yet built): no effects. An event can write
# its own rows and nothing else, so a sale cannot decrement stock. That is the
# obvious next case and the one that will drive the shape.
#
# DEFERRED: no migrations section, so a pack cannot yet declare that a column
# appears on day 40. The drift operations themselves are not in this repository
# yet; the two should arrive together so the timeline is designed against the
# operations that will execute it.
