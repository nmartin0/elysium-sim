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

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
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
class SavedClock:
    """The only thing the databases cannot tell us."""

    now: datetime

    def write(self, directory: Path) -> Path:
        path = Path(directory) / STATE_FILENAME
        partial = path.with_suffix(".json.part")
        partial.write_text(json.dumps({"clock": self.now.isoformat()}, indent=2) + "\n")
        # Renamed into place, like everything else this project
        # publishes: a half-written state file read by a resume would
        # be worse than none.
        partial.replace(path)
        return path

    @classmethod
    def read(cls, directory: Path) -> "SavedClock":
        path = Path(directory) / STATE_FILENAME
        if not path.exists():
            raise ResumeError(
                f"no {STATE_FILENAME} in {directory}; this world was never stopped "
                f"cleanly, so there is no record of what time it had reached"
            )
        try:
            saved = json.loads(path.read_text())
            return cls(now=datetime.fromisoformat(saved["clock"]))
        except (ValueError, KeyError, OSError) as error:
            raise ResumeError(f"{path} is not readable: {error}") from error


def save(world: World, directory: Path) -> Path:
    """Record what the databases cannot."""
    return SavedClock(now=world.clock.now()).write(directory)


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
            rows = fetch_all(
                world.silo(where.silo), world.database(where.silo),
                f"SELECT {_quote(world, where.silo, key.name)}, "
                f"{_quote(world, where.silo, where.state_column)} "
                f"FROM {_quote(world, where.silo, where.table)}",
            )
        except Exception as error:  # noqa: BLE001 -- driver errors differ per engine
            raise ResumeError(
                f"cannot read {where.silo}.{where.table} to rebuild {name}: {error}"
            ) from error

        states = world.pack.lifecycles[name].states
        # DWELL RESTARTS, and this is the one thing a resume does not
        # recover. An entity's state is in the database; WHEN it
        # entered that state is not -- persistence declares an id
        # column and a state column and nothing else -- so every
        # entity looks freshly arrived and any transition gated on
        # dwell waits its full time again.
        #
        # Said here rather than discovered: a world built from twelve
        # monthly legs restarts every entity's clock twelve times, so
        # progression across a boundary is slower than it would have
        # been in one sitting. An optional `entered_at` column on the
        # persistence block would fix it and is the right answer when
        # somebody needs it.
        arrived = world.clock.now()
        live = []
        for identifier, state in rows:
            if str(state) not in states:
                # A row whose state is not one this lifecycle knows --
                # an archived record from a migration, say. It is real
                # data and it is not a live entity, so it is left in
                # the table and out of the simulation.
                continue
            live.append(Entity(lifecycle=name, entity_id=str(identifier),
                               state=str(state), entered_state_at=arrived,
                               created_at=arrived))
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
    saved = SavedClock.read(directory)
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
