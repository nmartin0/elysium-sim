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

from datetime import datetime
from pathlib import Path

from simulator.clock import DEFAULT_COMPRESSION, SimulatedClock
from simulator.generators import build as build_generator
from simulator.ports import PortRegistry
from simulator.relational import apply_schema, create_database, insert_rows, verify_schema
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
    schema = world.pack.schemas[step.silo]
    table = schema.table(step.table)
    generators = {name: build_generator(spec) for name, spec in step.columns.items()}
    # A stream per table, so adding a column to one seed step does not
    # shift the values another produces.
    context = world.context(f"seed.{step.qualified}")

    rows = []
    for _ in range(step.count):
        for column_name, generator in generators.items():
            # In DECLARED order, which is what lets a later column refer
            # to an earlier one through the context's row.
            context.set_field(column_name, generator.value(context))
        rows.append(context.finish_row(step.qualified))

    return insert_rows(world.silo(step.silo), world.database(step.silo), table, rows)


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
# DEFERRED (known, intentional, not yet built): there is no tick. Nothing
# advances the clock or makes anything happen after seeding, because events,
# triggers and emissions are not in the spec model yet. build + seed is a
# complete and useful thing on its own -- it produces populated databases a
# consumer can be pointed at -- and it is what the event layer will be added to.
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
