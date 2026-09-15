"""
cli.py  (running a simulation without writing a script)

Until now the only way to run a world was to import the package and
write Python. That is a fine way to test the simulator and a useless
way to hand somebody a database to point at, which is the entire
product.

TWO VERBS, because there are two things people do:

  check  -- read a pack and say whether it is valid, without building
            anything. Fast enough to run on every save while writing
            one, and the errors already name the path.
  run    -- build the world, seed it, simulate, and then STAY UP.

THE STAYING UP IS THE POINT. A simulator whose databases vanish when
the script returns has produced nothing anyone can connect to. `run`
holds the silos open until interrupted, so the thing a consumer needs
-- a live endpoint -- outlives the process that made it. Ctrl-C shuts
them down cleanly rather than leaking clusters that hold their ports.

CONNECTIONS ARE WRITTEN TO A FILE, not just printed. A port that has
to be copied out of a terminal is a port somebody mistypes; a consumer
should be able to read connections.json and configure itself. It is
written before the simulation starts, so a consumer can be waiting on
it, and rewritten at the end in case anything moved.

--follow RUNS IN REAL TIME, which is what makes a consumer watchable
rather than merely pointed at a finished pile. The clock already knows
how to do this (`advance_real`, and a compression factor saying how
many simulated seconds pass per real one); nothing had ever asked it
to.
"""

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from types import FrameType

from simulator import runner
from simulator.clock import DEFAULT_COMPRESSION
from simulator.spec import PackError, load_pack
from simulator.world import World

#: Written into the world directory. A consumer reads this instead of
#: being told a port by hand.
CONNECTIONS_FILENAME = "connections.json"

#: How often --follow pushes the simulation forward, in real seconds.
#: Small enough that a watching consumer sees a steady trickle rather
#: than bursts, large enough not to spend the run committing.
FOLLOW_INTERVAL_SECONDS = 1.0


class _Interrupted(Exception):
    """Ctrl-C, turned into something a finally block can act on."""


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "check":
            return _check(arguments)
        return _run(arguments)
    except BrokenPipeError:
        # `simulator check pack.yaml | head` is an ordinary thing to
        # do, and without this it ends in a traceback -- which looks
        # like the pack was broken rather than the pipe. Found by a dry
        # run doing exactly that.
        #
        # stdout is pointed at devnull before returning because Python
        # flushes it at exit, which would raise the same error again
        # after main() has finished and print a second traceback
        # nothing can catch.
        _silence_stdout()
        return 0


def _silence_stdout() -> None:
    import os

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, sys.stdout.fileno())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simulator",
        description="Run a simulated business as real databases.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser(
        "check", help="validate a pack file without building anything")
    check.add_argument("pack", type=Path)

    run = commands.add_parser(
        "run", help="build a world, simulate it, and keep it reachable")
    run.add_argument("pack", type=Path)
    run.add_argument("--dir", type=Path, default=Path("var"),
                     help="where to build the world (default: ./var)")
    run.add_argument("--days", type=float, default=7.0,
                     help="simulated days to run before serving (default: 7)")
    run.add_argument("--tick", type=float, default=3600.0,
                     help="simulated seconds per tick (default: 3600)")
    run.add_argument("--seed", type=int, default=1,
                     help="random seed; the same seed gives the same world")
    run.add_argument("--compression", type=float, default=DEFAULT_COMPRESSION,
                     help=f"simulated seconds per real second when following "
                          f"(default: {DEFAULT_COMPRESSION:g})")
    run.add_argument("--follow", action="store_true",
                     help="keep simulating in real time after the backfill")
    run.add_argument("--stop-after", action="store_true",
                     help="tear the world down instead of staying up")
    return parser


# -- check -----------------------------------------------------------

def _check(arguments: argparse.Namespace) -> int:
    try:
        pack = load_pack(arguments.pack)
    except (PackError, OSError) as error:
        print(f"{arguments.pack}: {error}", file=sys.stderr)
        return 1

    print(f"{pack.name}: {pack.description or 'no description'}")
    for name, silo in sorted(pack.silos.items()):
        detail = f" ({silo.database})" if silo.database else ""
        tables = pack.schemas[name].tables if name in pack.schemas else ()
        print(f"  silo    {name:12} {silo.kind}{detail}"
              f"{f', {len(tables)} tables' if tables else ''}")
    for name in sorted(pack.lifecycles):
        states = len(pack.lifecycles[name].states)
        print(f"  cycle   {name:12} {states} states")
    for event in pack.events:
        print(f"  event   {event.name:12} {type(event.trigger).__name__}"
              f", {len(event.emissions)} emissions")
    for migration in pack.migrations:
        days = migration.at_seconds / 86400
        print(f"  drift   day {days:<8g} {migration.change.describe()}")
    return 0


# -- run -------------------------------------------------------------

def _run(arguments: argparse.Namespace) -> int:
    try:
        pack = load_pack(arguments.pack)
    except (PackError, OSError) as error:
        print(f"{arguments.pack}: {error}", file=sys.stderr)
        return 1

    print(f"Building {pack.name} in {arguments.dir}")
    world = runner.build(pack, arguments.dir, seed=arguments.seed)
    # Written BEFORE the simulation starts, so a consumer waiting on
    # the file can connect while the backfill is still running rather
    # than after it.
    path = write_connections(world, arguments.dir)
    _report(world, path)

    interrupted = False
    try:
        with _interruptible():
            runner.seed(world)
            if arguments.days > 0:
                print(f"\nSimulating {arguments.days:g} days...")
                written = runner.run(world, total_seconds=arguments.days * 86400,
                                     tick_seconds=arguments.tick)
                print(f"  {written} rows written, "
                      f"clock at {world.clock.now():%Y-%m-%d %H:%M}")

            if arguments.stop_after:
                return 0
            if arguments.follow:
                _follow(world, arguments.compression, arguments.tick)
            else:
                print("\nWorld is up. Press Ctrl-C to stop.")
                signal.pause()
    except _Interrupted:
        interrupted = True
    finally:
        # Rewritten in case anything moved, then everything shut down.
        # A leaked cluster holds its port, so the next run of the same
        # world fails on a conflict unrelated to whatever went wrong.
        write_connections(world, arguments.dir)
        print("\nStopping..." if interrupted else "")
        runner.stop(world)
    return 0


def _follow(world: World, compression: float, tick_seconds: float) -> None:
    """Keep simulating in real time until interrupted.

    The clock has always known how to do this and nothing had ever
    asked it: `compression` says how many simulated seconds pass per
    real second, so a day of trade can be watched in twenty minutes.
    """
    print(f"\nFollowing at {compression:g}x. Press Ctrl-C to stop.")
    step = min(tick_seconds, FOLLOW_INTERVAL_SECONDS * compression)
    while True:
        started = time.monotonic()
        runner.tick(world, step)
        # Sleep for what is LEFT of the interval, so a slow tick
        # catches up rather than compounding a drift between the
        # simulated clock and the wall clock.
        remaining = (step / compression) - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)


# -- connections -----------------------------------------------------

def write_connections(world: World, directory: Path) -> Path:
    """Write every silo's connection details where a consumer can read them.

    A port copied out of a terminal is a port somebody mistypes.
    """
    payload = {
        "pack": world.pack.name,
        "silos": {
            name: {"kind": descriptor.kind, **descriptor.details}
            for name, descriptor in world.connections().items()
        },
    }
    path = Path(directory) / CONNECTIONS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written to a temporary name and renamed, for the same reason the
    # file-drop silo publishes that way: a consumer polling for this
    # file must not read half of it.
    partial = path.with_suffix(".json.part")
    partial.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    partial.replace(path)
    return path


def _report(world: World, path: Path) -> None:
    print(f"\nConnections (also written to {path}):")
    for name, descriptor in sorted(world.connections().items()):
        print(f"  {name:12} {descriptor.summary()}")


class _interruptible:
    """Turn Ctrl-C into an exception a finally block can act on.

    signal.pause() and time.sleep() both need the handler to raise for
    the teardown below to run at all; the default handler would raise
    KeyboardInterrupt, which works, but says "Traceback" to somebody
    who did exactly what the prompt told them to.
    """

    def __enter__(self) -> "_interruptible":
        self._previous = signal.signal(signal.SIGINT, self._raise)
        return self

    def __exit__(self, *_: object) -> None:
        signal.signal(signal.SIGINT, self._previous)

    @staticmethod
    def _raise(_signal: int, _frame: FrameType | None) -> None:
        raise _Interrupted


if __name__ == "__main__":
    sys.exit(main())


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: `run` stays up by default rather than exiting. A simulator whose
# databases vanish when the process returns has produced nothing anyone can
# connect to, which is the entire product. --stop-after is there for scripted
# use, where the world is built, inspected and discarded.
#
# RESOLVED: connections.json is written BEFORE the simulation starts and again
# at the end. A consumer waiting on the file can connect during the backfill
# rather than after it, which is also the more realistic shape -- a real system
# is not empty when you first point something at it.
#
# RESOLVED: --follow uses the clock's compression, which had existed since the
# first commit with no caller. Sleeping for what is LEFT of the interval means
# a slow tick catches up instead of compounding a drift between simulated and
# wall time.
#
# DEFERRED (known, intentional, not yet built): no `clean` verb. A world
# directory holds a cluster per silo, and an interrupted run leaks them along
# with their ports. Stopping them again needs the pack that built them, which
# `clean` would have to re-read -- workable, and worth doing deliberately
# rather than bolting on here.
#
# DEFERRED: no way to attach to a world that is already running. Two terminals,
# one running and one inspecting, is an obvious want; it needs the second
# process to reach silos the first one owns, which the ports file almost
# supports already.
#
# DEFERRED: --follow cannot be told to stop at a simulated date, so a scenario
# like "follow until day 40, then apply this drift" has to be scripted in
# Python. That is the scenario layer, and it should be designed as one.
