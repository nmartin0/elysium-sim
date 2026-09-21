"""
effects.py  (changing a number that is already there)

An emission writes a row. An effect changes one that exists -- a
customer's balance going up when they are invoiced and down when they
pay -- and the difference matters because a balance is not a fact
anybody recorded, it is the running total of everything that has
happened to it.

The arithmetic happens in the database, `SET x = x + %s`, rather than
by reading a value and writing it back. Read-then-write is wrong the
moment two things touch the same row in one tick, and it is wrong in
the way that is hardest to see: the number is merely a little off, and
only sometimes.

A floor is available because a balance that goes negative because two
payments landed together is a support call, not a business fact.
"""

from typing import Any

from simulator.event import AdjustEffect
from simulator.generators import GeneratorError, build
from simulator.spec.references import _check_event_reference
from simulator.spec.schemas import NUMERIC_TYPES
from simulator.spec.scope import EventContext
from simulator.spec.values import PackError, _require_mapping, _string


def _load_effect(definition: Any, path: str, context: EventContext) -> AdjustEffect:
    definition = _require_mapping(definition, path)
    unknown = sorted(set(definition) - {"adjust", "by", "where", "floor"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")

    target = _string(definition, "adjust", path)
    if target.count(".") != 2:
        raise PackError(path, f"{target!r} must be written as silo.table.column")
    silo_name, table_name, column_name = target.split(".")
    if silo_name not in context.schemas:
        raise PackError(path, f"no schema is declared for silo {silo_name!r}")
    try:
        table = context.schemas[silo_name].table(table_name)
        column = table.column(column_name)
    except KeyError as error:
        raise PackError(path, str(error)) from error
    if column.type not in NUMERIC_TYPES:
        raise PackError(
            path,
            f"{target!r} is {column.type.value}, which cannot be adjusted; "
            f"adjustable types are {sorted(t.value for t in NUMERIC_TYPES)}"
        )

    if "by" not in definition:
        raise PackError(path, "needs a `by` saying how much to add")
    by = _effect_generator(definition["by"], f"{path}.by", context)

    where_raw = definition.get("where")
    if not isinstance(where_raw, dict) or not where_raw:
        # Without one, the adjustment moves every row in the table.
        raise PackError(path, "needs a `where` to say which rows to adjust")
    where = {}
    for key, declaration in where_raw.items():
        if key not in {column.name for column in table.columns}:
            raise PackError(f"{path}.where", f"table {table_name!r} has no column {key!r}")
        where[key] = _effect_generator(declaration, f"{path}.where.{key}", context)

    return AdjustEffect(silo=silo_name, table=table_name, column=column_name,
                        by=by, where=where, floor=definition.get("floor"))

def _effect_generator(declaration: Any, path: str, context: EventContext):
    """Build a generator for an effect, checking what it may refer to.

    An effect runs after every emission has finished its rows, so the
    context's own row is empty -- `row` and bare names refer to
    nothing. Only the subject and what has been emitted are available,
    and saying so at load is better than a reference error from inside
    a tick.
    """
    try:
        generator = build(declaration)
    except GeneratorError as error:
        raise PackError(path, str(error)) from error
    for reference in sorted(generator.references()):
        root = reference.split(".")[0]
        if root in {"row", "picked"} or "." not in reference:
            raise PackError(
                path,
                f"cannot refer to {reference!r} in an effect: effects run after every "
                f"emission has finished its row, so only `subject` and `emitted` are "
                f"available"
            )
        _check_event_reference(reference, {}, path, context)
    return generator
