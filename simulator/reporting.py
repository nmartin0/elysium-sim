"""
reporting.py  (the three verbs that read a world somebody else is running)

`status`, `drift` and `audit` are what an operator types in a second
terminal while a simulation is going on. None of them changes
anything, and all three answer a different version of the same
question: what is this world doing right now.

THEY READ THE ENGINE, NOT THE PACK, and that is the whole reason they
are trustworthy. `status` asks the catalogue what tables exist rather
than what the pack declared, so a column a migration added shows up
and one a migration dropped does not. `audit` reads the statement logs
the engines themselves write, so it reports what a consumer ACTUALLY
did rather than what it was permitted to do. Asking the pack would be
asking the simulator to mark its own homework.

`drift` is the exception that proves it: it changes the world on
purpose, using the same vocabulary a pack declares migrations in --
shared code, not a second dialect, because a console with its own
words for the same operations is two vocabularies to keep in step.
"""

import argparse
import sys
from typing import Any

import yaml

from simulator.attaching import attach, reach
from simulator.silo import SiloError


def _status(arguments: argparse.Namespace) -> int:
    from simulator.drift import history
    from simulator.relational import read_schema

    try:
        published = attach(arguments.dir)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    print(f"pack     {published['pack']}")
    for name, details in sorted(published["silos"].items()):
        silo = reach(name, details, arguments.dir)
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
        published = attach(arguments.dir)
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

    silo = reach(silo_name, details, arguments.dir)
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

def _audit(arguments: argparse.Namespace) -> int:
    """What each account did, read from the engines' own logs.

    Not "what was it allowed to do" -- the grants answer that. A tool
    that never issues a DROP and a tool whose DROP was refused look
    identical from outside, and only one of them is reassuring.
    """
    from simulator.audit import DANGEROUS, consumers_only, read_silo, summarise

    try:
        published = attach(arguments.dir)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    shown = False
    for name, details in sorted(published["silos"].items()):
        if details.get("database") is None:
            continue
        statements = read_silo(reach(name, details, arguments.dir))
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
