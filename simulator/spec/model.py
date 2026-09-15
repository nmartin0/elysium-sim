"""
model.py  (a pack file, parsed -- data, and nothing else)

Frozen dataclasses. Nothing here reads a file, validates a
cross-reference, or runs anything; loader.py does all three. The split
is the same one schema.py and dialect.py draw, and for the same
reason: a parsed pack should be inspectable and comparable without
dragging in the machinery that produced it.

WHY THESE ARE NOT ONE BASE CLASS. They share no behaviour. A SiloSpec
and a LifecycleSpec have nothing in common except having been read
from the same file, and a `Spec` parent would carry nothing but its
own name. This is the counter-example to the generator hierarchy in
the same project: there, a real contract with unrelated
implementations; here, unrelated data with no contract.

WHAT IS DELIBERATELY ABSENT: events, triggers, emissions and effects.
They are the largest part of the eventual vocabulary and none of them
can be written yet, because they all need the world layer to say what
an event happens TO. Declaring their shape now would be guessing, and
a spec model is exactly the wrong place to guess -- every pack file
written against it would have to change.
"""

from dataclasses import dataclass, field

from simulator.drift import SchemaChange
from simulator.event import Event
from simulator.lifecycle import Lifecycle
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


@dataclass(frozen=True)
class SeedStep:
    """Reference data to write once, before any simulation runs.

    Stores, products, departments, aircraft: the slow-changing things
    that exist before any activity happens.
    """

    silo: str
    table: str
    #: How many rows to write, when the step stands alone.
    count: int
    #: "silo.table" to seed one row PER ROW of another table, instead of
    #: a fixed count. Without this a pack cannot key one reference table
    #: to another -- seeding inventory for the products it just seeded
    #: was impossible, because the two steps share an id counter and
    #: produced different skus.
    per: str | None
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

    The id column is NOT declared. It is the table's primary key, which
    the schema already states; asking a pack to repeat it would be a
    second place for the two to disagree.
    """

    silo: str
    table: str
    id_column: str
    state_column: str

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
    #: In declared order, though nothing depends on the order BETWEEN
    #: events -- only on the order of emissions within one.
    events: tuple[Event, ...] = ()
    #: In the order they are due. Validated at load by applying them
    #: in sequence to the declared schema, so a pack that drops a
    #: column twice fails when the file is read.
    migrations: tuple[Migration, ...] = ()

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
# back until the world layer existed to say what an event happens TO. The shape
# they took -- a subject being a ROW of a declared table -- is drawn from that,
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
