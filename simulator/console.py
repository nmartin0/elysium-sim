"""
console.py  (driving a live world from a prompt)

A simulation you can only configure before it starts is a fixture
generator with extra steps. The useful thing is to have a consumer
connected, watching, and then do something to it -- advance a week,
drop a column, rescale a money field -- and see what the consumer
makes of it. That is the whole product, and until now it needed a
Python script.

    > status
    > advance 3d
    > drift rescale_column table=ops.invoices column=total factor=100
    > history

The drift syntax is the pack'S syntax. `drift add_column
table=ops.orders column=channel type=text length=16` is the migration
vocabulary with the `at:` removed, built by the same function the
loader uses. A console with its own words for the same operations
would be two vocabularies to learn and two to keep in step, and the
second one would drift.

It reads FROM stdin, not a terminal. Which means a scenario can be
piped in:

    printf 'advance 7d\\ndrift drop_column table=ops.orders column=note\\nadvance 1d\\n' \\
        | simulator run pack.yaml --console

That is the scenario layer arriving through the back door, and it is a
better back door than a config format: the commands are the same ones
somebody types by hand, so a scenario is a transcript rather than a
separate thing to design.

Commands are a dict, not an if-chain, matching the four other
registries in this codebase (generators, silos, drift operations,
migration builders). Adding one means adding an entry.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from simulator import runner
from simulator.drift import history
from simulator.oracle import Oracle, Watch
from simulator.silo import SiloError
from simulator.spec import PackError, build_change
from simulator.world import World

PROMPT = "sim> "


class ConsoleExit(Exception):
    """The operator asked to stop."""


@dataclass
class Console:
    """A prompt over one running world.

    `read` and `write` are injected so the console can be driven by a
    test, a pipe or a person without knowing which. That is not a
    testing seam bolted on -- piping a scenario in is a first-class way
    to use this.
    """

    world: World
    write: Callable[[str], Any] = print
    read: Callable[[str], str] | None = None

    def run(self) -> None:
        """Read commands until end of input or `quit`."""
        for line in self._lines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                self.execute(line)
            except ConsoleExit:
                return
            except Exception as error:  # noqa: BLE001 -- a prompt must not die
                # A console that exits on a typo is worse than useless
                # when a consumer is attached to the world it was
                # holding open.
                self.write(f"error: {error}")

    def _lines(self) -> Iterator[str]:
        reader = self.read or input
        while True:
            try:
                yield reader(PROMPT)
            except (EOFError, KeyboardInterrupt, StopIteration):
                # StopIteration as well as EOFError, because driving the
                # console from an iterator of lines is a natural thing
                # to do and PEP 479 would otherwise turn it into a
                # RuntimeError from inside this generator -- an
                # obscure failure for an obvious usage.
                return

    def execute(self, line: str) -> None:
        name, _, rest = line.partition(" ")
        handler = COMMANDS.get(name)
        if handler is None:
            raise ConsoleError(
                f"unknown command {name!r}; try one of {', '.join(sorted(COMMANDS))}"
            )
        handler(self, rest.strip())


class ConsoleError(Exception):
    """A command was malformed."""


def _fields(rest: str) -> dict[str, Any]:
    """Parse `key=value key=value` into a mapping.

    Values are read as YAML scalars so `length=16` is an integer and
    `nullable=false` is a boolean -- the same coercion a pack file
    gets, for the same words.
    """
    import yaml

    fields: dict[str, Any] = {}
    for token in rest.split():
        if "=" not in token:
            raise ConsoleError(f"expected key=value, got {token!r}")
        key, _, value = token.partition("=")
        fields[key] = yaml.safe_load(value)
    return fields


# -- commands ---------------------------------------------------------

def _help(console: Console, rest: str) -> None:
    console.write("commands:")
    for name in sorted(COMMANDS):
        console.write(f"  {name:12} {HELP[name]}")


def _status(console: Console, rest: str) -> None:
    world = console.world
    console.write(f"pack     {world.pack.name}")
    console.write(f"clock    {world.clock.now():%Y-%m-%d %H:%M} "
                  f"({world.clock.elapsed.days}d elapsed)")
    for name in sorted(world.silos):
        silo = world.silo(name)
        state = "up" if silo.is_reachable() else "DOWN"
        console.write(f"silo     {name:12} {silo.kind:11} {state}")
    for lifecycle, entities in sorted(world.entities.items()):
        counts: dict[str, int] = {}
        for entity in entities:
            counts[entity.state] = counts.get(entity.state, 0) + 1
        summary = ", ".join(f"{state}={count}" for state, count in sorted(counts.items()))
        console.write(f"entities {lifecycle:12} {summary}")


def _connections(console: Console, rest: str) -> None:
    for name, descriptor in sorted(console.world.connections().items()):
        console.write(f"  {name:12} {descriptor.summary()}")


def _advance(console: Console, rest: str) -> None:
    from simulator.spec.values import _duration

    if not rest:
        raise ConsoleError("advance needs a duration, as in `advance 3d`")
    seconds = _duration(rest.split()[0], "advance")
    written = runner.run(console.world, total_seconds=seconds, tick_seconds=3600)
    console.write(f"advanced to {console.world.clock.now():%Y-%m-%d %H:%M}, "
                  f"{written} rows written")


def _drift(console: Console, rest: str) -> None:
    """Apply a schema change right now.

    The point of the whole console: a consumer is connected and
    watching, and this is the moment the ground moves.
    """
    operation, _, arguments = rest.partition(" ")
    if not operation:
        raise ConsoleError("drift needs an operation, as in "
                           "`drift drop_column table=ops.orders column=note`")
    fields = _fields(arguments)
    if "table" not in fields:
        raise ConsoleError("drift needs table=silo.table")
    silo_name = str(fields["table"]).split(".")[0]
    world = console.world
    try:
        change = build_change(operation, fields, world.schema(silo_name))
    except (PackError, KeyError) as error:
        raise ConsoleError(str(error)) from error

    world.schemas[silo_name] = change.apply(
        world.silo(silo_name), world.database(silo_name),
        world.schema(silo_name), world.clock.now(),
    )
    # Sampled immediately, so the oracle records the moment the ground
    # moved rather than whenever the next tick happens to come round.
    # Without this, `drift` followed by `oracle` reports the value from
    # before the drift, which is the most misleading possible answer at
    # the most interesting possible moment.
    world.oracle.sample(world)
    console.write(f"applied: {change.describe()}"
                  f"{'  (BREAKING)' if change.is_breaking else ''}")


def _history(console: Console, rest: str) -> None:
    world = console.world
    found = False
    for name, spec in sorted(world.pack.silos.items()):
        if spec.database is None:
            continue
        try:
            entries = history(world.silo(name), spec.database)
        except SiloError:
            # Nothing has drifted in this silo. Narrowed from Exception
            # so that a genuine failure is not silently skipped over on
            # its way to looking like an undrifted silo.
            continue
        for entry in entries:
            found = True
            mark = "BREAKING" if entry["breaking"] else "additive"
            console.write(f"  {entry['applied_at']:%Y-%m-%d %H:%M}  {name:10} "
                          f"{mark:9} {entry['detail']}")
            console.write(f"    really at {entry['occurred_at']:%H:%M:%S}, "
                          f"which is the clock the statement log uses")
    if not found:
        console.write("  nothing has drifted yet")


def _watch(console: Console, rest: str) -> None:
    """Start watching a number, so a later drift can be judged against it."""
    if rest.count(".") != 2:
        raise ConsoleError("watch needs silo.table.column, as in "
                           "`watch ops.invoices.total`")
    silo, table, column = rest.split(".")
    watch = Watch(silo=silo, table=table, column=column)
    console.world.oracle = Oracle(watches=(*console.world.oracle.watches, watch))
    console.world.oracle.sample(console.world)
    console.write(f"watching {watch.name}, now {console.world.oracle.latest(watch)}")


def _oracle(console: Console, rest: str) -> None:
    oracle = console.world.oracle
    if not oracle.watches:
        console.write("  nothing is being watched; try `watch ops.invoices.total`")
        return
    for watch in oracle.watches:
        samples = oracle.series.get(watch.name, [])
        jumps = oracle.jumps(watch, Decimal(100)) + oracle.jumps(watch, Decimal("0.01"))
        blind = oracle.went_blind(watch)
        note = ""
        if blind is not None:
            note = f"  went blind at {blind:%Y-%m-%d %H:%M}"
        elif jumps:
            note = f"  {len(jumps)} hundredfold step(s) -- nothing raised"
        console.write(f"  {watch.name} = {oracle.latest(watch)} "
                      f"({len(samples)} samples){note}")


def _quit(console: Console, rest: str) -> None:
    raise ConsoleExit


COMMANDS: dict[str, Callable[[Console, str], None]] = {
    "help": _help,
    "status": _status,
    "connections": _connections,
    "advance": _advance,
    "drift": _drift,
    "history": _history,
    "watch": _watch,
    "oracle": _oracle,
    "quit": _quit,
}

HELP = {
    "help": "list these commands",
    "status": "clock, silos, and what state entities are in",
    "connections": "where each silo can be reached",
    "advance": "move time forward, as in `advance 3d`",
    "drift": "change the schema now, as in "
             "`drift drop_column table=ops.orders column=note`",
    "history": "every schema change applied, and how badly",
    "watch": "track a number, as in `watch ops.invoices.total`",
    "oracle": "what the watched numbers are, and whether they jumped",
    "quit": "stop the world and exit",
}


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: the console's drift syntax is the pack's migration syntax with `at:`
# removed, built by the same function the loader calls. A console with its own
# words for the same operations would be two vocabularies to keep in step, and
# the second would drift. This is why loader.build_change and
# MIGRATION_OPERATIONS became public.
#
# RESOLVED: read and write are injected. Not a testing seam bolted on -- piping
# a scenario in is a first-class way to use this, and a scenario is then a
# transcript of what somebody would have typed rather than a separate format to
# design.
#
# RESOLVED: the loop catches everything. A prompt that exits on a typo is worse
# than useless when a consumer is attached to the world it was holding open.
#
# RESOLVED: `drift` samples the oracle immediately after applying. Without it,
# `drift` followed by `oracle` reported the value from before the drift -- the
# most misleading possible answer at the most interesting possible moment,
# because the oracle otherwise only samples on a tick and a manual drift is not
# one.
#
# DEFERRED (known, intentional, not yet built): no `seed` command, so reference
# data cannot be added to a running world. It would need the same
# per-table generator machinery the pack uses, driven from arguments, and the
# shape of that is worth taking from a real want rather than guessing.
#
# DEFERRED: `advance` always ticks hourly regardless of how far it is asked to
# go, so `advance 365d` is 8,760 ticks and takes a while. A tick size argument
# is trivial; what is not obvious is whether a coarser tick should be allowed
# to change the arrivals, and the honest answer is that it does not -- so this
# is a convenience rather than a correctness question.
#
# DEFERRED: no way to attach a console to a world another process is running.
# Two terminals -- one running, one poking -- is the obvious next want, and it
# needs the second process to reach silos the first one owns. The ports file
# almost supports it already.
