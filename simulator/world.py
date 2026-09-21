"""
world.py  (one running simulation, and everything it shares)

A World is a pack that has been brought into existence: its silos are
running, its databases exist, its schemas are applied, and its clock
is at some moment. Everything that has to be shared is held here and
handed to whatever needs it, rather than assembled independently in
several places -- two subsystems holding different clocks, or drawing
from unrelated random streams, is how a run stops being reproducible.

The counters are the subtle part. Id generators need a counter that
persists across every event in the run, but an EvaluationContext is
built fresh for each one. So the world owns the dict and passes the
same object into every context. That is also what makes a resumed run
possible later: the counters are in one place, owned by something that
outlives a single event.

What this is not: a scheduler. The world holds state; something else
decides what happens next. Keeping that line means the world can be
inspected, snapshotted and torn down without anything running, which
is what every test here does.
"""

from dataclasses import dataclass, field
from datetime import datetime

from simulator.clock import SimulatedClock
from simulator.context import EvaluationContext
from simulator.lifecycle import Entity
from simulator.oracle import Oracle
from simulator.ports import PortRegistry
from simulator.rng import RandomSource
from simulator.scheduler import EventCalendar
from simulator.schema import Schema
from simulator.silo import ConnectionDescriptor, Silo
from simulator.spec import PackSpec


@dataclass
class World:
    """One pack, running."""

    pack: PackSpec
    clock: SimulatedClock
    rng: RandomSource
    #: Live silos by the business's name for them.
    silos: dict[str, Silo]
    ports: PortRegistry
    #: Id counters by prefix, shared into every EvaluationContext. See
    #: the module note for why they live here rather than on a context.
    counters: dict[str, int] = field(default_factory=dict)
    calendar: EventCalendar = field(default_factory=EventCalendar)
    #: An independent record of what was true and when. Empty unless a
    #: When each replica was last rebuilt, in elapsed simulated
    #: seconds. On the world because it belongs to one: a module-level
    #: dict keyed by id(world) was tried and is a latent bug -- CPython
    #: reuses an id the moment an object is collected (measured: 1,998
    #: reuses in 2,000 short-lived objects), so a new world would
    #: inherit a previous one's refresh times and skip refreshing until
    #: its clock passed them.
    replica_refreshed: dict[str, float] = field(default_factory=dict)

    #: caller declares watches -- the oracle costs a query per watch
    #: per tick, and a world nobody is checking should not pay for it.
    oracle: Oracle = field(default_factory=Oracle)
    #: The schema as it is now, which is not the pack's declared
    #: schema once a migration has run. Initialised from the pack and
    #: revised by drift; everything that reads a table's shape at
    #: runtime must read this, or a pack would keep writing to a column
    #: its own migration dropped.
    schemas: dict[str, Schema] = field(default_factory=dict)
    #: Live entities by lifecycle name. Created by emissions declaring
    #: `spawns`, advanced by the runner on every tick.
    entities: dict[str, list[Entity]] = field(default_factory=dict)
    #: Transitions that happened in the tick currently running, for
    #: TransitionTrigger to read. Filled by the runner before events
    #: fire and cleared after, so a transition fires its events once
    #: and only in the tick it happened in.
    transitions: list[dict] = field(default_factory=list)
    #: Rows of tables events are `per`, read once. See subject_rows.
    _subject_cache: dict[str, list[dict]] = field(default_factory=dict)

    # -- access ------------------------------------------------------

    def silo(self, name: str) -> Silo:
        if name not in self.silos:
            raise KeyError(f"no silo {name!r} in this world; it has {sorted(self.silos)}")
        return self.silos[name]

    def database(self, silo_name: str) -> str:
        """The database name inside a relational silo.

        Held on the pack rather than the silo because it is a fact the
        business declared, not one the technology knows.
        """
        declared = self.pack.silo(silo_name).database
        if declared is None:
            raise KeyError(f"silo {silo_name!r} does not hold a database")
        return declared

    def connections(self) -> dict[str, ConnectionDescriptor]:
        """Where every silo can be reached, and which database to use.

        The only thing a consumer needs from a running world, and the
        reason it is a first-class method rather than something to be
        assembled by whoever is looking: a port that has to be dug out
        of a log is a port nobody uses.

        The declared database is passed for relational silos, not the
        default. Left alone they answer with the maintenance database
        -- `postgres` and `mysql` -- which exists, accepts connections,
        and contains none of the business's data. A consumer following
        that descriptor would connect successfully to the wrong place
        and find nothing, which is a far worse failure than not
        connecting at all.
        """
        from dataclasses import replace as _replace

        from simulator.silos.reader import READER, WRITER, password_for

        connections = {}
        for name, silo in self.silos.items():
            database = self.pack.silo(name).database
            descriptor = (
                silo.connection(database) if database is not None else silo.connection()
            )
            if database is not None:
                # Added here rather than by the silo, because a
                # credential belongs to a world and a silo has no seed
                # to derive one from. Plain text on purpose: a consumer
                # has to read it from somewhere, the data is fictional,
                # and what the password buys is that a consumer must
                # actually SEND one.
                descriptor = _replace(descriptor, details={
                    **descriptor.details,
                    "password": password_for(READER, self.rng.run_seed),
                    "writer_password": password_for(WRITER, self.rng.run_seed),
                })
            connections[name] = descriptor
        return connections

    def schema(self, silo_name: str) -> Schema:
        """This silo's schema as it stands, drift included."""
        if silo_name not in self.schemas:
            raise KeyError(
                f"no schema for silo {silo_name!r}; this world has {sorted(self.schemas)}"
            )
        return self.schemas[silo_name]

    def register(self, entity: Entity) -> Entity:
        self.entities.setdefault(entity.lifecycle, []).append(entity)
        return entity

    def living(self, lifecycle: str) -> list[Entity]:
        return self.entities.get(lifecycle, [])

    def spawn(self, lifecycle_name: str, entity_id: str) -> Entity:
        """A new entity in its lifecycle's initial state, registered.

        The id is the PRIMARY KEY of the row that created it, not a
        fresh number. That is what lets the runner find the row again
        when the entity moves, without a second mapping that could fall
        out of step with the database.
        """
        lifecycle = self.pack.lifecycles[lifecycle_name]
        now = self.clock.now()
        return self.register(Entity(
            entity_id=entity_id,
            lifecycle=lifecycle.name,
            state=lifecycle.initial,
            entered_state_at=now,
            created_at=now,
        ))

    def forget_subject_rows(self) -> None:
        """Drop the cache, because the tables behind it have changed.

        The cache is right for reference data, which is written once
        and read forever. It is wrong the moment something writes to a
        table it holds -- and seeding does exactly that, including to a
        table an earlier seed step picked from.

        Found for real: a step seeding duplicate customers PICKED from
        the customer table, which filled the cache with the thirty rows
        that existed at that moment. Every event afterwards was `per`
        that stale list, so the three duplicates never raised a single
        job and looked like records nobody had ever called about.
        """
        self._subject_cache.clear()

    def subject_rows(self, qualified: str) -> list[dict]:
        """Rows of a table an event happens to, read once and cached.

        Cached because a rate trigger asks on every tick, and a query
        per tick per event would dominate a run. The tables events are
        `per` are reference data -- products, stores, technicians --
        which change rarely; the cache is invalidated by nothing yet,
        and that is a real limit stated in the notes below rather than
        hidden.
        """
        if qualified in self._subject_cache:
            return self._subject_cache[qualified]
        if qualified.count(".") != 1:
            raise KeyError(f"{qualified!r} must be written as silo.table")
        silo_name, table_name = qualified.split(".")
        table = self.schema(silo_name).table(table_name)

        from simulator.dialect import dialect_for
        from simulator.relational import fetch_all

        dialect = dialect_for(self.silo(silo_name).kind)
        names = [column.name for column in table.columns]
        columns = ", ".join(dialect.quote(name) for name in names)
        rows = fetch_all(self.silo(silo_name), self.database(silo_name),
                         f"SELECT {columns} FROM {dialect.quote(table_name)}")
        self._subject_cache[qualified] = [dict(zip(names, row, strict=True)) for row in rows]
        return self._subject_cache[qualified]

    # -- evaluation --------------------------------------------------

    def context(self, stream: str, subject: dict | None = None,
                now: datetime | None = None) -> EvaluationContext:
        """A fresh context for generating one row or one event.

        `stream` names the random stream, so that two unrelated parts
        of a pack draw independently and adding a draw to one does not
        shift the other. The counters dict is passed by reference on
        purpose -- see the module note.
        """
        return EvaluationContext(
            now=now or self.clock.now(),
            rng=self.rng.stream(stream),
            subject=subject,
            counters=self.counters,
        )


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): counters live on the World and are passed by
# reference into every EvaluationContext. Id generators need a counter that
# outlives a single event; a context does not. Keeping them in one place owned
# by something long-lived is also what makes a resumed run possible.
#
# RESOLVED: the world holds state and does not schedule. Something else decides
# what happens next, which is what lets a world be built, inspected and torn
# down without anything running -- every test in tests/test_world.py does
# exactly that.
#
# RESOLVED: the registry is back, with a real caller -- an emission declaring
# `spawns` creates an entity, and the runner advances it. Deleting it when
# nothing created entities was right rather than churn: the shape it has now
# (an id that is the row's primary key) came from seeing how emissions
# actually create things, and would have been guessed wrong before.
#
# RESOLVED (kept for history): World carried an entity registry -- entities,
# register(), living(), spawn() and lifecycle() -- and it was deleted before
# commit. Nothing called any of it: entities are created by events, and events
# are not in the spec model yet. Vulture found it, and the rule this project
# applies is that code whose only users are its own tests is speculative. It
# returns with the event layer, where each method will have a real caller and
# the shape can be drawn from how events actually create things rather than
# from a guess.
#
# DEFERRED: subject_rows caches forever and nothing invalidates it. That is
# correct for reference data -- the products and stores an event is `per` --
# and wrong the moment a pack makes an event `per` a table it also writes to,
# where the cache would hide the new rows. Detecting that at load is possible
# (a pack declaring an event per a table that some emission inserts into) and
# is the right guard; it is not written because no pack does it yet.
#
# DEFERRED (known, intentional, not yet built): nothing here persists. Counters
# and the calendar live in memory for the run's duration, so a second
# process opening the same silos finds databases full of history and no live
# entities. An earlier prototype measured that precisely: a backfill over an
# already-built world produced 3,360 sales with not one attributable to a
# customer. The fix there was to rebuild entity state from the databases
# themselves, which is the right answer here too and belongs with whatever
# declares where a lifecycle's state is persisted.
#
# DEFERRED: no snapshot or restore. A scenario wanting to branch a world -- run
# to day 40, then try two different drift events from the same point -- needs
# one, and it is a real want. It needs the silos to snapshot too, which for
# PostgreSQL means a filesystem copy of a stopped cluster.
