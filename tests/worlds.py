"""
worlds.py  (building a world from a pack written inline)

A MODULE OF ITS OWN, NOT conftest. `write_pack` was byte-identical in
seven test files and the `world` fixture appeared in eleven, several
identical and the rest differing only in seed, duration and engine --
so it wanted sharing. Putting it in conftest and importing it by name
did not work: tests/consumer has its own conftest, and with both
directories on the path the two shadow each other, which surfaced as a
collection error only when the whole suite ran at once.

Importing from conftest is a pytest anti-pattern for exactly this
reason. conftest is for fixtures pytest discovers; a helper other
modules import should be a module they can name.

These are helpers rather than fixtures because what each file wants
differs: some want a world seeded and run, some only built, one wants
two to compare. A fixture would have to take all of that as parameters
and would read worse than the three lines it replaced.
"""

from contextlib import contextmanager

from simulator import runner
from simulator.spec import load_pack


def write_pack(tmp_path, source, name="pack"):
    """A pack file on disk, loaded. Tests declare their YAML inline."""
    path = tmp_path / f"{name}.yaml"
    path.write_text(source)
    return load_pack(path)


@contextmanager
def running_world(tmp_path, source, name="pack", *, seed=1, days=0,
                  tick_seconds=1800):
    """A world built from inline YAML, torn down however the test ends.

    MODULE SCOPE IS SAFE ONLY WHERE NO TEST MUTATES, and which files
    those are was checked mechanically rather than by eye -- by
    looking for runner.run, runner.tick, inserts, drift and terminate
    in every test that takes the fixture. Measured: test_picking paid
    4.2 seconds of cluster setup five times over, and test_exposing
    three seconds nine times, for worlds none of their tests changed.
    A file with even one mutator keeps a fresh world per test, or
    gives that one test its own.

    `days` of 0 builds and seeds without simulating, which several
    tests want -- they run their own spans and would be confused by a
    backfill they did not ask for. Getting this wrong during the
    consolidation is what made four tests fail: the span was read from
    the whole old file rather than from the fixture's own body, so
    fixtures that never ran acquired a backfill.
    """
    world = runner.build(write_pack(tmp_path, source, name), tmp_path / name, seed=seed)
    try:
        runner.seed(world)
        if days:
            runner.run(world, total_seconds=days * 86400, tick_seconds=tick_seconds)
        yield world
    finally:
        runner.stop(world)
