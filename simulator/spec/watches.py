"""
watches.py  (the numbers a pack says are worth checking)

The oracle keeps an independent record of what was true and when, and
it is the only thing that can catch a RESCALE: a migration that
multiplies a column by a hundred raises nothing, breaks no type, and
leaves every read succeeding. The difference between a consumer that
noticed and one that did not is a record of what the number was
before.

UNTIL NOW THAT RECORD COULD ONLY BE ASKED FOR IN PYTHON, which means
the person running a training world could not ask for it at all. A
business knows which of its numbers matter -- total invoiced, balance
owed, what the engineers earned -- and the pack is where it says so.

    watches:
      - {silo: dispatch, table: invoices, column: total}
      - {silo: dispatch, table: customers, column: balance_owed,
         aggregate: max}

EVERY PART IS CHECKED AGAINST THE SCHEMA, because a watch naming a
column that is not there would sample nothing and report a number that
never existed -- and the oracle's whole value is being trustworthy
about the past.
"""

from typing import Any

from simulator.oracle import AGGREGATES, Watch
from simulator.schema import ColumnType, Schema
from simulator.spec.values import PackError, _require_mapping, _string

#: What can be aggregated. `count` is the exception: it counts rows
#: and does not care what is in them, so it is the only aggregate a
#: text column can take.
_MEASURABLE = frozenset({ColumnType.DECIMAL, ColumnType.INTEGER, ColumnType.BIGINT})


def load_watches(raw: Any, schemas: dict[str, Schema]) -> tuple[Watch, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise PackError("watches", "must be a non-empty list of watches")

    watches = []
    for index, entry in enumerate(raw):
        path = f"watches[{index}]"
        entry = _require_mapping(entry, path)
        unknown = sorted(set(entry) - {"silo", "table", "column", "aggregate"})
        if unknown:
            raise PackError(path, f"does not understand {unknown}")

        silo_name = _string(entry, "silo", path)
        if silo_name not in schemas:
            raise PackError(
                path,
                f"no schema is declared for silo {silo_name!r}; this pack has "
                f"{sorted(schemas)}"
            )
        table_name = _string(entry, "table", path)
        try:
            table = schemas[silo_name].table(table_name)
        except KeyError as error:
            raise PackError(
                path, f"silo {silo_name!r} has no table {table_name!r}") from error

        column_name = _string(entry, "column", path)
        column = next((c for c in table.columns if c.name == column_name), None)
        if column is None:
            raise PackError(
                path,
                f"table {table_name!r} has no column {column_name!r}; it has "
                f"{[c.name for c in table.columns]}"
            )

        aggregate = str(entry.get("aggregate", "sum"))
        if aggregate not in AGGREGATES:
            raise PackError(
                path,
                f"{aggregate!r} is not an aggregate; available: {sorted(AGGREGATES)}"
            )
        if aggregate != "count" and column.type not in _MEASURABLE:
            # summing a text column is a driver error at the first
            # tick, and a min or max over one answers a question about
            # alphabetical order that nobody asked.
            raise PackError(
                path,
                f"cannot take {aggregate!r} of {column_name!r}, which is "
                f"{column.type.value}; only count works on a column that is not a "
                f"number"
            )

        watches.append(Watch(silo=silo_name, table=table_name, column=column_name,
                             aggregate=aggregate))

    names = [watch.name for watch in watches]
    if len(set(names)) != len(names):
        raise PackError("watches", "declares the same watch twice")
    return tuple(watches)


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: watches are checked against the schema at load. One naming a column
# that is not there would sample nothing and report a number that never existed,
# and the oracle's whole value is being trustworthy about the past.
#
# RESOLVED: only `count` is allowed on a non-numeric column. Summing text is a
# driver error at the first tick; min or max over it answers a question about
# alphabetical order that nobody asked.
#
# DEFERRED (known, intentional, not yet built): a watch cannot be filtered --
# there is no way to say "the total of unvoided invoices". That is the number a
# business actually cares about, and the unfiltered one moves whenever something
# is voided, which is drift-shaped noise in a record meant to reveal drift.
