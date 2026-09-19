"""
resume.py  (picking a world up where it was left)

WHAT THIS UNBLOCKS. Every run so far has produced days, because a
world exists only while the process that built it is alive. A
consumer asking "how did this change last quarter", or paging a year
of invoices, has nothing to work with -- and a year cannot be produced
in one sitting if stopping loses it.

An earlier prototype measured the cost of getting this wrong exactly:
a backfill over an already-built world produced 3,360 sales with not
one attributable to a customer, because the entities the events needed
were not there and nothing said so.

WHERE EACH PIECE COMES FROM, and they differ on purpose.

Entities are read from the DATABASES. A lifecycle's state is written
to a real column -- a work order whose `status` really does move from
quoted to completed -- so the database already holds it and a second
copy would be a second thing to disagree. Reading it back is not a
convenience; it is the only source that cannot go stale.

Id counters are DERIVED from the same rows, for the same reason. A
counter kept in a file could disagree with the data after anything
else wrote to it, and this simulator hands out a writer account
precisely so that something else can. The highest id in the table is
what the next one has to beat, whoever put it there.

The clock is the exception and is kept in a FILE. It has no home in
the business data: no column says what time the simulation thinks it
is, and inferring it from the newest row would guess differently
depending on which tables a pack happens to write. So it is recorded
explicitly, and the file says nothing else -- everything else would be
a copy of something the databases already know.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from simulator.lifecycle import Entity
from simulator.relational import fetch_all
from simulator.world import World

#: Written beside connections.json. Small on purpose -- see the module
#: note on why nothing else belongs in it.
STATE_FILENAME = "state.json"

#: The shape the id generator produces: a prefix, an underscore, and a
#: zero-padded number. Parsed rather than stored, so a counter cannot
#: drift from the rows it is meant to be ahead of.
_ISSUED_ID = re.compile(r"^(?P<prefix>.+)_(?P<number>\d+)$")


class ResumeError(Exception):
    """A world could not be picked up where it was left."""


@dataclass(frozen=True)
class SavedState:
    """What the databases cannot tell us, and who they belong to."""

    now: datetime
    #: The pack that built this world. Resuming a world with a
    #: different pack fails at the first write to a table that is not
    #: there, with a driver error naming a column -- which sends
    #: somebody looking at the database rather than at the command they
    #: just typed.
    pack: str
    #: A fingerprint of the world's schema AS IT STOOD, migrations
    #: included. Not the pack's declared schema: a world that has
    #: drifted legitimately differs from what its pack says, and
    #: comparing against the declaration would refuse every drifted
    #: world.
    shape: str

    def write(self, directory: Path) -> Path:
        path = Path(directory) / STATE_FILENAME
        partial = path.with_suffix(".json.part")
        partial.write_text(json.dumps(
            {"clock": self.now.isoformat(), "pack": self.pack, "shape": self.shape},
            indent=2) + "\n")
        # Renamed into place, like everything else this project
        # publishes: a half-written state file read by a resume would
        # be worse than none.
        partial.replace(path)
        return path

    @classmethod
    def read(cls, directory: Path) -> "SavedState":
        path = Path(directory) / STATE_FILENAME
        if not path.exists():
            raise ResumeError(
                f"no {STATE_FILENAME} in {directory}; this world was never stopped "
                f"cleanly, so there is no record of what time it had reached"
            )
        try:
            saved = json.loads(path.read_text())
            return cls(now=datetime.fromisoformat(saved["clock"]),
                       pack=str(saved.get("pack", "")),
                       shape=str(saved.get("shape", "")))
        except (ValueError, KeyError, OSError) as error:
            raise ResumeError(f"{path} is not readable: {error}") from error


def fingerprint(world: World) -> str:
    """A short, stable summary of every table and column in the world.

    NOT A DIFF, deliberately. This says "something is different" and
    the engine's own catalogue says what, which verify_schema is
    already good at. A fingerprint that tried to be readable would be
    a second, worse schema representation.

    Built from the world's schemas rather than the pack's, so a world
    that has drifted fingerprints as what it IS. Sorted, because
    dictionary order is not a fact about a database.
    """
    parts = []
    for silo_name in sorted(world.schemas):
        for table in sorted(world.schema(silo_name).tables, key=lambda t: t.name):
            for column in table.columns:
                parts.append(
                    f"{silo_name}.{table.name}.{column.name}:{column.type.value}"
                    f":{'null' if column.nullable else 'notnull'}"
                    f":{column.precision}:{column.scale}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def save(world: World, directory: Path) -> Path:
    """Record what the databases cannot."""
    return SavedState(now=world.clock.now(), pack=world.pack.name,
                      shape=fingerprint(world)).write(directory)


def restore_entities(world: World) -> dict[str, int]:
    """Rebuild the live entities from the rows that hold their state.

    Returns how many were found per lifecycle, because a resume that
    silently found none looks exactly like a resume that worked until
    the first event fires.
    """
    found: dict[str, int] = {}
    for name, where in world.pack.persistence.items():
        table = world.schema(where.silo).table(where.table)
        key = table.primary_key()
        if key is None:
            raise ResumeError(
                f"{where.silo}.{where.table} has no primary key, so a {name} entity "
                f"cannot be identified in it"
            )
        try:
            selected = [key.name, where.state_column]
            if where.entered_column is not None:
                selected.append(where.entered_column)
            rows = fetch_all(
                world.silo(where.silo), world.database(where.silo),
                f"SELECT {', '.join(_quote(world, where.silo, name) for name in selected)} "
                f"FROM {_quote(world, where.silo, where.table)}",
            )
        except Exception as error:  # noqa: BLE001 -- driver errors differ per engine
            raise ResumeError(
                f"cannot read {where.silo}.{where.table} to rebuild {name}: {error}"
            ) from error

        states = world.pack.lifecycles[name].states
        # DWELL SURVIVES ONLY IF THE PACK SAID WHERE IT IS WRITTEN.
        # With `entered_column` the moment comes back from the database
        # and an entity picks up mid-dwell. Without it, every entity
        # looks freshly arrived -- which is what every world did before
        # the column existed, and is still what a pack that omits it
        # gets. A year built from twelve legs then restarts every
        # entity's clock twelve times.
        arrived = world.clock.now()
        live = []
        for row in rows:
            identifier, state = row[0], row[1]
            # WHEN it was entered, if the pack said where to find it.
            # Without that column every entity looks freshly arrived,
            # so any transition gated on dwell waits its full time
            # again and a year built from twelve legs restarts every
            # entity's clock twelve times.
            entered = row[2] if where.entered_column is not None else None
            if entered is not None and entered.tzinfo is None:
                entered = entered.replace(tzinfo=UTC)
            if str(state) not in states:
                # A row whose state is not one this lifecycle knows --
                # an archived record from a migration, say. It is real
                # data and it is not a live entity, so it is left in
                # the table and out of the simulation.
                continue
            live.append(Entity(lifecycle=name, entity_id=str(identifier),
                               state=str(state),
                               entered_state_at=entered or arrived,
                               created_at=entered or arrived))
        world.entities[name] = live
        found[name] = len(live)
    return found


def restore_counters(world: World) -> dict[str, int]:
    """Set each id counter past the highest id already issued.

    Derived rather than stored, because a counter in a file could
    disagree with the data after anything else wrote to it -- and this
    simulator hands out a writer account precisely so that something
    else can.
    """
    highest: dict[str, int] = {}
    for silo_name, schema in world.schemas.items():
        if world.pack.silos[silo_name].database is None:
            continue
        for table in schema.tables:
            key = table.primary_key()
            if key is None:
                # A table with no key issued no ids worth counting.
                continue
            try:
                rows = fetch_all(
                    world.silo(silo_name), world.database(silo_name),
                    f"SELECT {_quote(world, silo_name, key.name)} "
                    f"FROM {_quote(world, silo_name, table.name)}",
                )
            except Exception:  # noqa: BLE001 -- a table that cannot be read has no ids
                continue
            for (identifier,) in rows:
                match = _ISSUED_ID.match(str(identifier))
                if match is None:
                    # Not an id this simulator issued. A pack may use
                    # natural keys, and guessing a counter from one
                    # would be inventing a number.
                    continue
                prefix, number = match.group("prefix"), int(match.group("number"))
                highest[prefix] = max(highest.get(prefix, 0), number)
    world.counters.update(highest)
    return highest


def _quote(world: World, silo_name: str, identifier: str) -> str:
    from simulator.dialect import dialect_for

    return dialect_for(world.silo(silo_name).kind).quote(identifier)


def resume(world: World, directory: Path) -> dict[str, int]:
    """Put a freshly attached world back where the last one left off.

    The caller has already started the silos; this restores what lives
    above them. Raises rather than continuing quietly if the clock is
    missing, because a world resumed to the wrong date writes rows that
    look like the real ones and are not.
    """
    saved = SavedState.read(directory)
    if saved.pack and saved.pack != world.pack.name:
        raise ResumeError(
            f"{directory} holds a {saved.pack!r} world and this is {world.pack.name!r}. "
            f"Resuming with the wrong pack fails later, at the first write to a table "
            f"that is not there, with a driver error naming a column."
        )
    if saved.shape and saved.shape != fingerprint(world):
        raise ResumeError(
            "the pack's schema has changed since this world was built. Its tables "
            "and columns no longer match what is in the databases, and a resume "
            "would write rows that do not fit. Build a new world, or put the pack "
            "back the way it was."
        )
    # The clock's start moves rather than its elapsed time, so a
    # resumed world counts from where the last one stopped and every
    # `elapsed`-based question -- which migrations are due, how far a
    # run has got -- is answered against this leg rather than the
    # whole history.
    world.clock.start = saved.now
    world.clock._elapsed = timedelta()

    counters = restore_counters(world)
    entities = restore_entities(world)
    if not any(entities.values()) and world.pack.persistence:
        raise ResumeError(
            "no live entities were found in any table, so every lifecycle would "
            "start empty. An earlier prototype hit exactly this and produced 3,360 "
            "sales with not one attributable to a customer."
        )
    return {**entities, **{f"counter:{k}": v for k, v in counters.items()}}


def existing_world(directory: Path) -> bool:
    """Whether there is something here to resume."""
    return (Path(directory) / STATE_FILENAME).exists()


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: entities and counters come from the DATABASES and only the clock
# comes from a file. A lifecycle's state is already written to a real column and
# an id is already in a primary key, so a second copy would be a second thing to
# disagree -- and after the writer account exists, something other than the
# simulator can move both.
#
# RESOLVED: a resume that finds no entities raises rather than continuing. That
# failure is indistinguishable from a working resume until the first event
# fires, and an earlier prototype produced 3,360 unattributable sales that way.
#
# DEFERRED: dwell restarts on resume, because persistence declares an id column
# and a state column and nothing about WHEN a state was entered. An optional
# `entered_at` column on the persistence block would fix it. The cost is
# stated in restore_entities rather than left to be found: a year built from
# twelve legs restarts every entity's clock twelve times.
#
# DEFERRED (known, intentional, not yet built): the oracle's series is not
# restored, so a resumed world starts its record of what was true from empty.
# The samples are in memory only; persisting them needs a decision about where
# they live, since they are the simulator's own observations rather than the
# business's data.
#
# DEFERRED: nothing checks that the pack has not changed since the world was
# built. Resuming into a pack with different tables would fail confusingly at
# the first write rather than clearly at startup. A hash of the schema in the
# state file would catch it.
