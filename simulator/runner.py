"""
runner.py  (turning a pack file into running databases with rows in them)

Three operations, in the order they happen:

  build(pack)  -- allocate ports, start silos, create databases, apply
                  schemas, and verify that the engines agree
  seed(world)  -- write the reference data the pack declares
  stop(world)  -- shut every silo down

ORDER MATTERS AND IS NOT OBVIOUS. Ports are allocated for the whole
world before any silo starts, because allocating one at a time lets
the kernel hand the same ephemeral port to two of them. Schemas are
applied only after every silo is up, so a world with one broken silo
fails before any database has been half-built.

TEARDOWN IS BEST-EFFORT AND SAYS SO. stop() keeps going after a silo
fails to shut down, and reports what failed at the end. Stopping at the
first failure would leave the rest running -- and a leaked PostgreSQL
cluster holds its port, so the next run of the same world fails on a
conflict that has nothing to do with what went wrong.

SEEDING GENERATES COLUMN BY COLUMN, IN DECLARED ORDER. That is what
makes a later column able to refer to an earlier one: each generated
value goes into the context's row before the next generator runs. A
pack declaring `line_total` before `quantity` gets a clear error rather
than a wrong number, which is the reason declaration order is honoured
rather than sorted.
"""

from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from simulator.clock import DEFAULT_COMPRESSION, SimulatedClock
from simulator.generators import build as build_generator
from simulator.lifecycle import advance as advance_entity
from simulator.ports import PortRegistry
from simulator.relational import (
    apply_schema,
    create_database,
    fetch_rows_by_key,
    insert_rows,
    set_column,
    verify_schema,
)
from simulator.rng import RandomSource
from simulator.silo import Silo, SiloError
from simulator.silos import SILO_TYPES, build_silo
from simulator.spec import PackSpec, SeedStep
from simulator.world import World


def build(pack: PackSpec, data_dir: Path, *, seed: int = 1,
          start: datetime | None = None,
          compression: float = DEFAULT_COMPRESSION) -> World:
    """Bring a pack into existence. Leaves every silo running."""
    data_dir = Path(data_dir)
    # Which silos need a port is a property of their KIND, read off the
    # type. An earlier version built every silo first and asked the
    # instances -- which cannot work, because constructing a
    # port-requiring silo without a port is itself an error, so the
    # question could never be asked.
    #
    # Allocated for the whole world at once: one at a time lets the
    # kernel hand the same ephemeral port to two silos.
    registry = PortRegistry.allocate_names(data_dir, [
        name for name, spec in pack.silos.items()
        if SILO_TYPES[spec.kind].requires_port
    ])
    silos = _construct(pack, data_dir, registry)

    started: list[Silo] = []
    try:
        for silo in silos.values():
            silo.create()
            silo.start()
            started.append(silo)
        for silo_name, schema in pack.schemas.items():
            database = pack.silo(silo_name).database
            assert database is not None  # the loader guarantees this
            create_database(silos[silo_name], database)
            apply_schema(silos[silo_name], database, schema)
            verify_schema(silos[silo_name], database, schema)
    except Exception:
        # A half-built world is worse than none: its ports are held and
        # its clusters are running, so the next attempt fails on a
        # conflict rather than on the real problem.
        for silo in started:
            try:
                silo.stop()
            except SiloError:
                pass
        raise

    return World(
        pack=pack,
        # A copy, because migrations revise it and the pack's own
        # declaration is what the file says rather than what is true
        # now.
        schemas=dict(pack.schemas),
        clock=SimulatedClock(start=start or _default_start(), compression=compression),
        rng=RandomSource(seed),
        silos=silos,
        ports=registry,
    )


def _construct(pack: PackSpec, data_dir: Path, registry: PortRegistry) -> dict[str, Silo]:
    """Instantiate every silo. Touches nothing on disk."""
    return {
        name: build_silo(
            kind=spec.kind,
            name=name,
            data_dir=data_dir / name,
            port=registry.ports.get(name),
            options=spec.options,
        )
        for name, spec in pack.silos.items()
    }


def _default_start() -> datetime:
    """Where simulated time begins when a caller does not say.

    A fixed date rather than the wall clock, because a run seeded the
    same way should produce the same timestamps -- and one that started
    at "now" could not.
    """
    from datetime import UTC

    return datetime(2026, 1, 1, tzinfo=UTC)


def seed(world: World) -> dict[str, int]:
    """Write the reference data the pack declares. Returns row counts."""
    written = {}
    for step in world.pack.seed:
        written[step.qualified] = _seed_step(world, step)
    return written


def _seed_step(world: World, step: SeedStep) -> int:
    schema = world.schema(step.silo)
    table = schema.table(step.table)
    generators = {name: build_generator(spec) for name, spec in step.columns.items()}
    # A stream per table, so adding a column to one seed step does not
    # shift the values another produces.
    context = world.context(f"seed.{step.qualified}")
    # One row per subject when the step declares `per`, so a pack can
    # key one reference table to another -- inventory to the products
    # it was just given -- which a fixed count cannot do, because two
    # steps generating ids draw from the same counter and produce
    # different keys.
    subjects: list[dict | None] = (
        list(world.subject_rows(step.per)) if step.per else [None] * step.count
    )

    rows = []
    for subject in subjects:
        context.subject = subject
        for column_name, generator in generators.items():
            # In DECLARED order, which is what lets a later column refer
            # to an earlier one through the context's row.
            context.set_field(column_name, generator.value(context))
        rows.append(context.finish_row(step.qualified))

    return insert_rows(world.silo(step.silo), world.database(step.silo), table, rows)


def tick(world: World, seconds: float) -> int:
    """Advance the world by an interval, firing every event. Rows written.

    The clock moves FIRST, so events see the time they are happening
    at rather than the time the interval started. At a one-minute tick
    that difference is invisible; at an hour it is the difference
    between an event at 17:00 drawing the evening peak's rate and the
    afternoon's.

    ONE DATABASE CONNECTION PER SILO PER TICK, held open across every
    event. Measured: opening a connection per statement costs 62.9ms
    against 0.42ms on one already open -- 149 times the cost of the
    query -- so a tick writing a few hundred rows spent almost all of
    its time in handshakes. It also makes a tick atomic per silo,
    which is more honest: everything that happened in one interval
    becomes visible together.
    """
    world.clock.advance(seconds)
    with ExitStack() as stack:
        for name, spec in world.pack.silos.items():
            if spec.database is not None:
                stack.enter_context(world.silo(name).session(spec.database))  # type: ignore[attr-defined]
        _apply_due_migrations(world, seconds)
        world.transitions = _advance_lifecycles(world, seconds)
        try:
            return sum(event.fire(world, seconds) for event in world.pack.events)
        finally:
            # Cleared however the tick ends, so nothing reading the
            # world BETWEEN ticks sees stale transitions. Not what
            # stops them firing twice -- the next tick reassigns the
            # list, so failing to clear has no effect on firing at all.
            # A test asserting once-only firing passed without any
            # clearing, which is how that distinction surfaced.
            world.transitions = []


def _apply_due_migrations(world: World, seconds: float) -> None:
    """Run any migration whose moment fell inside this interval.

    BEFORE events fire, so the rest of the tick sees the shape the
    migration left. Running them after would mean a tick's writes going
    into a table that, by the time anyone looked, no longer had those
    columns -- which is a confusing way to fail and not one a real
    migration causes.

    Which migrations are due is computed from the clock rather than
    remembered, the same way periodic events are: due if the moment
    falls in (start, now]. Nothing to reset when a run resumes, and
    a tick longer than the gap between two migrations applies both.
    """
    now = world.clock.elapsed.total_seconds()
    started = now - seconds
    for migration in world.pack.migrations:
        if started < migration.at_seconds <= now:
            world.schemas[migration.silo] = migration.change.apply(
                world.silo(migration.silo), world.database(migration.silo),
                world.schema(migration.silo), world.clock.now(),
            )


def _advance_lifecycles(world: World, seconds: float) -> list[dict]:
    """Move every entity, and write the ones that moved.

    BEFORE events fire, so an event sees the states entities are in
    now rather than the ones they were in last tick. The alternative
    -- advance after -- means an entity that became `approved` this
    tick is still `quoted` to everything that runs in it, which is a
    full tick of lag nobody declared.

    Only entities that actually moved are written. A state machine
    where most things sit still would otherwise issue one UPDATE per
    entity per tick, which is the kind of cost that does not show up
    until a pack has thousands of them.

    Returns the transitions that fired, for TransitionTrigger to read.
    """
    now = world.clock.now()
    transitions: list[dict] = []
    for lifecycle_name, lifecycle in world.pack.lifecycles.items():
        moved: list[dict] = []
        rng = world.rng.stream(f"lifecycle.{lifecycle_name}")
        where = world.pack.persistence.get(lifecycle_name)
        for entity in world.living(lifecycle_name):
            previous = advance_entity(lifecycle, entity, now, seconds, rng)
            if previous is None:
                continue
            moved.append({
                # The id under the name the persisted table calls it,
                # so an update emission can address the row directly.
                # A lifecycle with no persistence has no such name, so
                # it uses a neutral one.
                (where.id_column if where else "entity_id"): entity.entity_id,
                "state": entity.state,
                "previous_state": previous,
                # Prefixed, so it cannot collide with a real column
                # name a pack might legitimately reference.
                "_lifecycle": lifecycle_name,
            })
            if where is None:
                # A lifecycle with no persisted_to drives behaviour
                # without the business system having a column for it,
                # which is a legitimate thing for a pack to want.
                continue
            table = world.schema(where.silo).table(where.table)
            set_column(
                world.silo(where.silo), world.database(where.silo), table,
                where.state_column, entity.state,
                {where.id_column: entity.entity_id},
            )

        transitions.extend(_with_entity_rows(world, where, moved))
    return transitions


def _with_entity_rows(world: World, where, moved: list[dict]) -> list[dict]:
    """Merge each moved entity's own row into its transition.

    Without this a transition event can only write things derivable
    from the id: an invoice raised when a work order is invoiced could
    not say which CUSTOMER it was for, because the subject carried the
    work order's id and nothing else. Found by writing the first real
    pack, where the customer came out as the literal "unknown".

    Fetched in ONE query for the whole tick rather than one per
    entity. The row is read AFTER the state column was written, so
    `subject.status` is the state just entered and agrees with
    `subject.state`.
    """
    if where is None or not moved:
        return moved
    table = world.schema(where.silo).table(where.table)
    rows = fetch_rows_by_key(
        world.silo(where.silo), world.database(where.silo), table,
        where.id_column, [entry[where.id_column] for entry in moved],
    )
    merged = []
    for entry in moved:
        row = rows.get(entry[where.id_column], {})
        # The transition's own facts win over the row's columns, so a
        # table with a column called `state` cannot shadow them.
        merged.append({**row, **entry})
    return merged


def run(world: World, total_seconds: float, tick_seconds: float = 60.0) -> int:
    """Advance repeatedly. Returns rows written.

    Tick size is a run parameter, not a pack one: rates are per hour
    and arrivals are per interval, so a pack behaves the same however
    finely time is sliced. That independence is asserted directly in
    the tests rather than assumed.
    """
    if tick_seconds <= 0:
        raise ValueError(f"tick_seconds must be positive, got {tick_seconds}")
    written = 0
    remaining = total_seconds
    while remaining > 0:
        step = min(tick_seconds, remaining)
        written += tick(world, step)
        remaining -= step
    return written


def stop(world: World) -> None:
    """Shut every silo down. Best-effort, and reports what failed.

    Stopping at the first failure would leave the rest running, and a
    leaked PostgreSQL cluster holds its port -- so the next run of the
    same world fails on a conflict that has nothing to do with whatever
    actually went wrong.
    """
    failures = []
    for name, silo in world.silos.items():
        try:
            silo.stop()
        except SiloError as error:
            failures.append(f"{name}: {error}")
    if failures:
        raise SiloError("some silos did not stop cleanly:\n  " + "\n  ".join(failures))


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): build() asks SILO_TYPES which kinds need a port,
# rather than building every silo first and asking the instances. The latter was
# written first and cannot work: constructing a port-requiring silo without a
# port is itself an error, so the question could never be asked of an instance.
# Needing a port is a property of the technology, which is what requires_port
# being a ClassVar already says.
#
# RESOLVED: a failure part way through build() stops whatever it already
# started. A half-built world holds its ports and leaves clusters running, so
# the next attempt fails on a port conflict rather than on the real problem.
#
# RESOLVED: _default_start is a fixed date, not the wall clock. A run seeded
# the same way should produce the same timestamps, and one starting at "now"
# could not.
#
# RESOLVED (kept for history): tick() and run() exist now. The clock advances
# BEFORE events fire, so an event sees the time it happens at rather than the
# time the interval started -- invisible at a one-minute tick, and the
# difference between the evening peak and the afternoon at an hourly one.
#
# RESOLVED: tick() holds one connection per silo open across the whole
# interval. The cost was predicted in relational.py's own notes and became real
# the moment a pack with effects existed -- 62.9ms per statement against 0.42ms
# reused, measured on this machine. Fifty-second tests became a few seconds.
#
# DEFERRED: seeding builds every row in memory before inserting. A pack seeding
# a million rows would not survive that. The insert is already one statement per
# step, so the fix is to chunk both the generation and the write together, and
# it is not worth doing until a pack asks for a volume that needs it.
#
# DEFERRED: nothing writes to file-drop or REST silos yet. A pack can declare
# them and they start and are reachable, but only relational silos receive seed
# data, because a seed step names a table. Their equivalent -- publishing a file
# or a collection -- needs its own declaration shape.
