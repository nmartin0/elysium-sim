"""
cli.py  (the verbs, and which module answers each)

WHAT IS LEFT HERE is the parser and the two verbs that make something:
`check`, which reads a pack and says what it declares without building
anything, and `run`, which builds a world and stays up so somebody can
connect to it.

Everything else arrives at a world that already exists and lives
elsewhere: reporting.py has `status`, `drift` and `audit`, health.py
has the checks `verify` runs, cleanup.py has `clean`, and attaching.py
has the two things all of them need -- reading what a world published
about itself, and reaching a running silo without starting one.

THE SPLIT IS BY WHAT A VERB NEEDS, not by tidiness. A verb that builds
a world needs the pack; a verb that inspects one must not read the
pack at all, because the whole value of `status` and `audit` is that
they ask the engine rather than the declaration. Keeping those in
separate modules makes the wrong import visible rather than merely
discouraged.
"""

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from types import FrameType

from simulator import runner
from simulator.attaching import CONNECTIONS_FILENAME, attach
from simulator.clock import DEFAULT_COMPRESSION
from simulator.health import _CHECKS
from simulator.reporting import _audit, _drift, _status
from simulator.spec import PackError, load_pack
from simulator.world import World

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
        if arguments.command == "status":
            return _status(arguments)
        if arguments.command == "drift":
            return _drift(arguments)
        if arguments.command == "audit":
            return _audit(arguments)
        if arguments.command == "verify":
            return _verify(arguments)
        if arguments.command == "clean":
            return _clean(arguments)
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
    run.add_argument("--console", action="store_true",
                     help="open a prompt to advance time and apply drift by hand")
    run.add_argument("--stop-after", action="store_true",
                     help="tear the world down instead of staying up")
    run.add_argument("--resume", action="store_true",
                     help="pick up a world already in --dir rather than building one")

    status = commands.add_parser(
        "status", help="describe a world another terminal is running")
    status.add_argument("--dir", type=Path, default=Path("var"),
                        help="the world's directory (default: ./var)")

    drift = commands.add_parser(
        "drift", help="change a running world's schema from outside")
    drift.add_argument("operation",
                       help="add_column, drop_column, rescale_column, ...")
    drift.add_argument("fields", nargs="*", metavar="key=value",
                       help="table=silo.table column=... and so on")
    drift.add_argument("--dir", type=Path, default=Path("var"),
                       help="the world's directory (default: ./var)")

    audit = commands.add_parser(
        "audit", help="what each account actually did to the databases")
    audit.add_argument("--dir", type=Path, default=Path("var"),
                       help="the world's directory (default: ./var)")
    audit.add_argument("--account", help="show every statement by this account")
    audit.add_argument("--dangerous", action="store_true",
                       help="show only statements that could change or destroy")
    audit.add_argument("--all", action="store_true",
                       help="include the simulator's own accounts, not just consumers")

    clean = commands.add_parser(
        "clean", help="stop anything still running for a world nothing owns")
    clean.add_argument("--dir", type=Path, default=Path("var"),
                       help="the world's directory (default: ./var)")
    clean.add_argument("--remove", action="store_true",
                       help="delete the directory too, once nothing is using it")

    verify = commands.add_parser(
        "verify", help="check the silos are sound, the way a consumer would")
    verify.add_argument("--dir", type=Path, default=Path("var"),
                        help="the world's directory (default: ./var)")
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
        # A replica has no tables of its own to count -- its shape is
        # whatever it copies -- and printing nothing there reads as a
        # silo with an empty schema, which is a different and worrying
        # thing.
        if silo.replicates is not None:
            shape = (f", a copy of {silo.replicates}"
                     f" every {silo.refresh_seconds / 3600:g}h")
        else:
            shape = f", {len(tables)} tables" if tables else ""
        print(f"  silo    {name:12} {silo.kind}{detail}{shape}")
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

    from simulator import resume as resuming

    if arguments.resume:
        print(f"Resuming {pack.name} from {arguments.dir}")
        world = runner.attach(pack, arguments.dir, seed=arguments.seed)
        try:
            restored = resuming.resume(world, arguments.dir)
        except resuming.ResumeError as error:
            runner.stop(world)
            print(str(error), file=sys.stderr)
            return 1
        print(f"  clock at {world.clock.now():%Y-%m-%d %H:%M}")
        for what, count in sorted(restored.items()):
            print(f"  {what}: {count}")
    else:
        print(f"Building {pack.name} in {arguments.dir}")
        world = runner.build(pack, arguments.dir, seed=arguments.seed)
    # Written before the simulation starts, so a consumer waiting on
    # the file can connect while the backfill is still running rather
    # than after it.
    path = write_connections(world, arguments.dir)
    _report(world, path)

    interrupted = False
    try:
        with _interruptible():
            if not arguments.resume:
                # A resumed world is already seeded. Seeding again would
                # collide on every reference key it wrote the first time.
                runner.seed(world)
            if arguments.days > 0:
                print(f"\nSimulating {arguments.days:g} days...")
                written = runner.run(world, total_seconds=arguments.days * 86400,
                                     tick_seconds=arguments.tick)
                print(f"  {written} rows written, "
                      f"clock at {world.clock.now():%Y-%m-%d %H:%M}")

            if arguments.stop_after:
                return 0
            if arguments.console:
                from simulator.console import Console

                print("\nWorld is up. Type `help` for commands, `quit` to stop.")
                Console(world=world).run()
            elif arguments.follow:
                _follow(world, arguments.compression, arguments.tick)
            else:
                print("\nWorld is up. Press Ctrl-C to stop.")
                signal.pause()
    except _Interrupted:
        interrupted = True
    finally:
        # The clock, so this world can be picked up again. Written
        # before the silos stop, while there is still something to ask.
        try:
            resuming.save(world, arguments.dir)
        except OSError as error:
            print(f"could not record the clock: {error}", file=sys.stderr)
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
        # Sleep for what is left of the interval, so a slow tick
        # catches up rather than compounding a drift between the
        # simulated clock and the wall clock.
        remaining = (step / compression) - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)


# -- attaching to a world somebody else is running --------------------


def _clean(arguments: argparse.Namespace) -> int:
    """Pick up after a simulator that did not get to tidy up.

    A world stopped properly leaves nothing running -- measured, the
    ports are bindable the instant stop() returns. This is for the
    other case: the process was killed, and the servers it started
    carried on serving with nothing left that knew they existed.
    """
    from simulator import cleanup

    if not arguments.dir.exists():
        print(f"{arguments.dir} is not there", file=sys.stderr)
        return 1

    result = cleanup.clean(arguments.dir, remove=arguments.remove)
    if not result["found"]:
        print(f"Nothing is running for {arguments.dir}.")
    for stray in result["stopped"]:
        print(f"  stopped  {stray.kind} (pid {stray.pid})")
    for stray in result["stubborn"]:
        print(f"  WOULD NOT STOP  {stray.kind} (pid {stray.pid})")
        print(f"                  {stray.command[:96]}")

    if arguments.remove:
        if result["removed"]:
            print(f"Removed {arguments.dir}.")
        elif result["stubborn"]:
            print(f"Left {arguments.dir} alone: something is still writing to it.")
        else:
            print(f"Could not remove {arguments.dir}.", file=sys.stderr)
            return 1
    elif result["found"] and not result["stubborn"]:
        # Said explicitly, because the useful next question after
        # "stopped three servers" is whether the world is still there.
        print(f"{arguments.dir} is still on disk; --remove deletes it.")

    return 1 if result["stubborn"] else 0


def _verify(arguments: argparse.Namespace) -> int:
    """Check the silos the way a consumer would, and say so plainly.

    WHY THIS EXISTS. Somebody learning to connect a tool to these
    databases will hit a problem, and their first question is whether
    the fault is theirs or the trainer's. Without an answer they spend
    the afternoon in the wrong logs. This connects using only
    connections.json -- no world object, no pack -- and reports what it
    found.

    It checks what a handover document CLAIMS, so the two cannot drift
    apart: reachable, tables present and populated, every table with a
    primary key, the read account genuinely unable to write, the file
    drop holding complete files with the byte-order mark, and the API
    paging rather than stopping at its first page.
    """
    try:
        published = attach(arguments.dir)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    problems: list[str] = []
    for name, details in sorted(published["silos"].items()):
        checks = _CHECKS.get(details["kind"])
        if checks is None:
            continue
        print(f"{name} ({details['kind']})")
        for description, check in checks:
            try:
                note = check(name, details, arguments.dir)
            except Exception as error:  # noqa: BLE001 -- every failure is a finding
                problems.append(f"{name}: {description} -- {error}")
                print(f"  FAILED  {description}")
                print(f"          {error}")
                continue
            print(f"  ok      {description}{f'  ({note})' if note else ''}")

    print()
    if problems:
        print(f"{len(problems)} problem(s). The fault is in the silos, not in "
              f"whatever is reading them.")
        return 1
    print("All sound. If something is still not working, it is not these.")
    return 0


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
# RESOLVED: connections.json is written before the simulation starts and again
# at the end. A consumer waiting on the file can connect during the backfill
# rather than after it, which is also the more realistic shape -- a real system
# is not empty when you first point something at it.
#
# RESOLVED: --follow uses the clock's compression, which had existed since the
# first commit with no caller. Sleeping for what is left of the interval means
# a slow tick catches up instead of compounding a drift between simulated and
# wall time.
#
# RESOLVED: --console exists because a simulation you can only configure before
# it starts is a fixture generator with extra steps. The useful thing is a
# consumer connected and watching while the ground moves under it, and that
# needed a Python script until now.
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
