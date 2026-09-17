"""
cli.py  (running a simulation without writing a script)

Until now the only way to run a world was to import the package and
write Python. That is a fine way to test the simulator and a useless
way to hand somebody a database to point at, which is the entire
product.

Four verbs. Two describe a pack, two talk to a world somebody else is
already running:

  check  -- read a pack and say whether it is valid, without building
            anything. Fast enough to run on every save while writing
            one, and the errors already name the path.
  run    -- build the world, seed it, simulate, and then stay up.
  status -- what a world another terminal is running looks like now
  drift  -- change its schema, from here, while that terminal keeps
            simulating

The last two need nothing FROM the running process. `run` holds the
clock, the live entities and the oracle in memory, and none of that is
reachable from outside -- but the databases are, and drift is pure DDL
against a database. So a second terminal can break a schema while the
first keeps trading, which is the case worth having: a consumer is
attached, and you want to move the ground under it without stopping
anything.

The schema those two work against is read back from the engine rather
than taken from the pack file, because once anything has drifted the
pack no longer describes what is there.

Two things they cannot do, said plainly rather than discovered. They
cannot move the clock or spawn anything, because those live in the
other process's memory. And a drift applied from here is stamped with
wall time, because the simulated clock is not reachable -- so the
history reads in real time while the rows it describes read in
simulated time.

The staying up is the point. A simulator whose databases vanish when
the script returns has produced nothing anyone can connect to. `run`
holds the silos open until interrupted, so the thing a consumer needs
-- a live endpoint -- outlives the process that made it. Ctrl-C shuts
them down cleanly rather than leaking clusters that hold their ports.

Connections are written to a file, not just printed. A port that has
to be copied out of a terminal is a port somebody mistypes; a consumer
should be able to read connections.json and configure itself. It is
written before the simulation starts, so a consumer can be waiting on
it, and rewritten at the end in case anything moved.

--follow runs in real TIME, which is what makes a consumer watchable
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
from typing import Any

import yaml

from simulator import runner
from simulator.clock import DEFAULT_COMPRESSION
from simulator.silo import SiloError
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
        if arguments.command == "status":
            return _status(arguments)
        if arguments.command == "drift":
            return _drift(arguments)
        if arguments.command == "audit":
            return _audit(arguments)
        if arguments.command == "verify":
            return _verify(arguments)
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
    # Written before the simulation starts, so a consumer waiting on
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

def _attach(directory: Path) -> dict[str, Any]:
    """Read the connection descriptors a running world published."""
    path = Path(directory) / CONNECTIONS_FILENAME
    if not path.exists():
        raise FileNotFoundError(
            f"no {CONNECTIONS_FILENAME} in {directory}; is a world running there?"
        )
    return json.loads(path.read_text())


def _reach(name: str, details: dict, directory: Path) -> Any:
    """A silo object that can talk to an already-running silo.

    Constructed but never created or started: this process did not
    build the world and must not try to. The data directory is passed
    because the file-based kinds are reached by path, and is unused by
    the ones reached by port.
    """
    from simulator.silos import build_silo

    kind = details["kind"]
    port = details.get("port") or details.get("base_url", "").rsplit(":", 1)[-1]
    return build_silo(kind=kind, name=name, data_dir=Path(directory) / name,
                      port=int(port) if port else None)


def _status(arguments: argparse.Namespace) -> int:
    from simulator.drift import history
    from simulator.relational import read_schema

    try:
        published = _attach(arguments.dir)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    print(f"pack     {published['pack']}")
    for name, details in sorted(published["silos"].items()):
        silo = _reach(name, details, arguments.dir)
        state = "up" if silo.is_reachable() else "DOWN"
        print(f"silo     {name:12} {details['kind']:11} {state}")
        database = details.get("database")
        if database is None or state == "DOWN":
            continue
        # Read from the engine, not the pack: once anything has
        # drifted, the pack no longer describes what is there.
        for table in read_schema(silo, database).tables:
            print(f"  table  {table.name:20} "
                  f"{', '.join(column.name for column in table.columns)}")
        try:
            entries = history(silo, database)
        except SiloError:
            # No history table, which means nothing has drifted. A
            # perfectly ordinary state that used to end in a traceback.
            continue
        for entry in entries:
            mark = "BREAKING" if entry["breaking"] else "additive"
            print(f"  drift  {entry['applied_at']:%Y-%m-%d %H:%M} {mark:9} "
                  f"{entry['detail']}")
            # The real moment too, because that is the one the
            # statement log is stamped in and so the only key the two
            # records share.
            print(f"           (really at {entry['occurred_at']:%Y-%m-%d %H:%M:%S})")
    return 0


def _drift(arguments: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from simulator.relational import read_schema
    from simulator.spec import PackError, build_change

    try:
        published = _attach(arguments.dir)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    fields: dict[str, Any] = {}
    for token in arguments.fields:
        if "=" not in token:
            print(f"expected key=value, got {token!r}", file=sys.stderr)
            return 1
        key, _, value = token.partition("=")
        fields[key] = yaml.safe_load(value)

    table = str(fields.get("table", ""))
    if table.count(".") != 1:
        print("drift needs table=silo.table", file=sys.stderr)
        return 1
    silo_name = table.split(".")[0]
    if silo_name not in published["silos"]:
        print(f"no silo called {silo_name!r} in {arguments.dir}", file=sys.stderr)
        return 1

    details = published["silos"][silo_name]
    database = details.get("database")
    if database is None:
        print(f"silo {silo_name!r} holds no database to change", file=sys.stderr)
        return 1

    silo = _reach(silo_name, details, arguments.dir)
    try:
        schema = read_schema(silo, database)
        change = build_change(arguments.operation, fields, schema)
            # Stamped with wall time, not simulated time, and that is a
        # real limitation rather than an oversight: the simulated clock
        # lives in the process running the world, and this one cannot
        # see it. The history is still ordered and still attributable;
        # it just reads in real time while the rest of the row reads in
        # simulated time.
        change.apply(silo, database, schema, datetime.now(UTC))
    except (PackError, KeyError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    print(f"applied: {change.describe()}"
          f"{'  (BREAKING)' if change.is_breaking else ''}")
    # Said plainly, because it is the one thing this cannot do: the
    # running process holds its schema in memory and has just been
    # made wrong about it.
    print("note: the running simulation still believes the old schema; "
          "it will fail on its next write to a column that moved.")
    return 0


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
        published = _attach(arguments.dir)
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


def _sql_checks() -> list:
    from simulator.relational import fetch_all, read_schema

    def reachable(name, details, directory):
        silo = _reach(name, details, directory)
        if not silo.is_reachable():
            raise RuntimeError("the server is not answering")
        return None

    def tables_have_rows(name, details, directory):
        silo = _reach(name, details, directory)
        schema = read_schema(silo, details["database"])
        if not schema.tables:
            raise RuntimeError("the database has no tables in it")
        empty = []
        for table in schema.tables:
            count = fetch_all(silo, details["database"],
                              f"SELECT count(*) FROM {_quoted(details, table.name)}")[0][0]
            if count == 0:
                empty.append(table.name)
        if empty:
            raise RuntimeError(f"these tables are empty: {sorted(empty)}")
        return f"{len(schema.tables)} tables, all populated"

    def every_table_has_a_key(name, details, directory):
        silo = _reach(name, details, directory)
        scope = "public" if details["kind"] == "postgresql" else details["database"]
        # key_column_usage, not table_constraints: the latter comes back
        # empty for an account holding only SELECT, on both engines.
        keyed = {str(row[0]) for row in fetch_all(
            silo, details["database"],
            "SELECT DISTINCT table_name FROM information_schema.key_column_usage "
            "WHERE table_schema = %s", (scope,))}
        missing = [t.name for t in read_schema(silo, details["database"]).tables
                   if t.name not in keyed]
        if missing:
            raise RuntimeError(f"no primary key on {sorted(missing)}")
        return None

    def the_reader_cannot_write(name, details, directory):
        silo = _reach(name, details, directory)
        table = read_schema(silo, details["database"]).tables[0].name
        # Through the PUBLISHED account, not the simulator's own, or
        # this would prove nothing about what was handed over.
        import psycopg
        import pymysql

        drivers = {"postgresql": psycopg, "mariadb": pymysql}
        driver = drivers[details["kind"]]
        kwargs = ({"host": details["host"], "port": details["port"],
                   "dbname": details["database"], "user": details["user"]}
                  if details["kind"] == "postgresql" else
                  {"host": details["host"], "port": details["port"],
                   "database": details["database"], "user": details["user"]})
        connection = driver.connect(**kwargs)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {_quoted(details, table)}")
        except Exception:
            return "DELETE refused, as it should be"
        finally:
            connection.close()
        raise RuntimeError(f"the {details['user']!r} account was allowed to DELETE")

    return [("reachable", reachable),
            ("tables present and populated", tables_have_rows),
            ("every table has a primary key", every_table_has_a_key),
            ("the read account cannot write", the_reader_cannot_write)]


def _quoted(details: dict, name: str) -> str:
    return f'"{name}"' if details["kind"] == "postgresql" else f"`{name}`"


def _filedrop_checks() -> list:
    def files_are_complete(name, details, directory):
        folder = Path(details["path"])
        if not folder.exists():
            raise RuntimeError(f"{folder} is not there")
        partial = list(folder.glob("*.part"))
        if partial:
            raise RuntimeError(f"half-written files present: {[p.name for p in partial]}")
        files = sorted(folder.glob("*.csv"))
        if not files:
            # NOT a fault. A weekly export that is not due yet has
            # published nothing, and telling an engineer their trainer
            # is broken because of it would send them hunting a problem
            # that does not exist -- which is the exact failure this
            # command is meant to prevent.
            return "nothing published yet, which is fine if none is due"
        return f"{len(files)} files, none half-written"

    def the_encoding_is_as_advertised(name, details, directory):
        files = sorted(Path(details["path"]).glob("*.csv"))
        if not files:
            return "nothing to check yet"
        raw = files[0].read_bytes()
        if details.get("encoding") == "utf-8-sig" and not raw.startswith(b"\xef\xbb\xbf"):
            raise RuntimeError("advertised as utf-8-sig but the byte-order mark is missing")
        return str(details.get("encoding"))

    return [("files complete", files_are_complete),
            ("encoding as advertised", the_encoding_is_as_advertised)]


def _rest_checks() -> list:
    import json as _json
    import urllib.request

    def ask(details, path):
        request = urllib.request.Request(str(details["base_url"]) + path)
        request.add_header("Authorization", f"Bearer {details['token']}")
        with urllib.request.urlopen(request, timeout=5) as response:
            return _json.loads(response.read())

    def answering(name, details, directory):
        body = ask(details, "/v1/invoices")
        if not body.get("data"):
            raise RuntimeError("the feed is empty")
        return f"{len(body['data'])} records on the first page"

    def paging_works(name, details, directory):
        seen, path = 0, "/v1/invoices"
        for _ in range(200):
            body = ask(details, path)
            seen += len(body.get("data", []))
            if "cursor" not in body:
                return f"{seen} records across every page"
            path = f"/v1/invoices?cursor={body['cursor']}"
        raise RuntimeError("the cursor never ran out, which means it is not advancing")

    return [("answering", answering), ("pages to the end", paging_works)]


_CHECKS = {
    "postgresql": _sql_checks(),
    "mariadb": _sql_checks(),
    "filedrop": _filedrop_checks(),
    "rest": _rest_checks(),
}


def _audit(arguments: argparse.Namespace) -> int:
    """What each account did, read from the engines' own logs.

    Not "what was it allowed to do" -- the grants answer that. A tool
    that never issues a DROP and a tool whose DROP was refused look
    identical from outside, and only one of them is reassuring.
    """
    from simulator.audit import DANGEROUS, consumers_only, read_silo, summarise

    try:
        published = _attach(arguments.dir)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    shown = False
    for name, details in sorted(published["silos"].items()):
        if details.get("database") is None:
            continue
        statements = read_silo(_reach(name, details, arguments.dir))
        if not arguments.all:
            # Consumers by default: every statement the simulation
            # makes is a write, so including them buries the one line
            # an operator is looking for under thousands.
            statements = consumers_only(statements)
        if not statements:
            continue
        shown = True
        print(f"{name} ({details['kind']})")
        if arguments.account or arguments.dangerous:
            for statement in statements:
                if arguments.account and statement.account != arguments.account:
                    continue
                if arguments.dangerous and not statement.dangerous:
                    continue
                mark = "REFUSED" if statement.refused else statement.kind
                print(f"  {mark:12} {statement.account:10} {statement.text[:90]}")
            continue
        for account, counts in sorted(summarise(statements).items()):
            parts = ", ".join(f"{kind} {count}" for kind, count in sorted(counts.items()))
            # Said plainly, because it is the line an operator is
            # looking for.
            risky = sum(count for kind, count in counts.items() if kind in DANGEROUS)
            note = "" if not risky else f"   <- {risky} could change or destroy"
            print(f"  {account:12} {parts}{note}")
    if not shown:
        print("no consumer has touched these databases yet"
              if not arguments.all else
              "nothing has been logged; is a world running there?")
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
