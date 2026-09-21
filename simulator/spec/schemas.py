"""
schemas.py  (turning the tables section of a pack into a Schema)

The second piece out of the loader, and it comes second because it
needs only the scalar helpers -- nothing here knows what an event or a
lifecycle is.

What it really does is refuse things. Reading a column declaration is
four lines; the rest is the list of ways a declaration can be wrong in
a way the engine would not catch until a migration failed or a number
came back rounded. A DECIMAL with no precision is the sharpest of
those: MySQL silently makes it DECIMAL(10,0), so money declared that
way loses its pence and nothing anywhere says so.
"""

from typing import Any

from simulator.schema import Column, ColumnType, Schema, Table
from simulator.spec.model import SiloSpec
from simulator.spec.values import (
    PackError,
    _require_mapping,
    _string,
)

#: Types arithmetic can be done to. Asked by two different questions --
#: whether a column can be adjusted, and whether it can be rescaled --
#: and declared once so the two cannot disagree about what a number is.
NUMERIC_TYPES = frozenset({ColumnType.DECIMAL, ColumnType.INTEGER, ColumnType.BIGINT})


def _relational_kinds() -> set[str]:
    from simulator.dialect import DIALECTS

    return set(DIALECTS)


def _load_schemas(raw: dict, silos: dict[str, SiloSpec]) -> dict[str, Schema]:
    relational = _relational_kinds()
    schemas = {}
    for silo_name, definition in raw.items():
        path = f"schemas.{silo_name}"
        if silo_name not in silos:
            raise PackError(path, f"there is no silo called {silo_name!r}")
        kind = silos[silo_name].kind
        if kind not in relational:
            # A folder of CSV and a JSON API do not have tables. Letting
            # a pack declare a schema for one would produce a pack that
            # looks complete and creates nothing.
            raise PackError(
                path,
                f"silo {silo_name!r} is a {kind!r} silo and cannot hold tables; "
                f"schemas belong to {sorted(relational)} silos"
            )
        if silos[silo_name].database is None:
            raise PackError(
                path, f"silo {silo_name!r} holds a schema, so it must name a database"
            )
        tables = _require_mapping(definition, path).get("tables")
        if not isinstance(tables, dict) or not tables:
            raise PackError(path, "must declare at least one table under `tables`")
        schemas[silo_name] = Schema(tables=tuple(
            _load_table(table_name, table_def, f"{path}.tables.{table_name}")
            for table_name, table_def in tables.items()
        ))
    return schemas

def _load_table(name: str, definition: Any, path: str) -> Table:
    columns = _require_mapping(definition, path).get("columns")
    if not isinstance(columns, dict) or not columns:
        raise PackError(path, "must declare at least one column under `columns`")
    try:
        return Table(name=name, columns=tuple(
            _load_column(column_name, column_def, f"{path}.columns.{column_name}")
            for column_name, column_def in columns.items()
        ))
    except ValueError as error:
        raise PackError(path, str(error)) from error

def _load_column(name: str, definition: Any, path: str) -> Column:
    definition = _require_mapping(definition, path)
    type_name = _string(definition, "type", path)
    try:
        column_type = ColumnType(type_name)
    except ValueError as error:
        raise PackError(
            path,
            f"unknown column type {type_name!r}; available: "
            f"{sorted(member.value for member in ColumnType)}"
        ) from error

    unknown = sorted(set(definition) - {"type", "nullable", "primary_key", "length",
                                        "precision", "scale"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")

    try:
        return Column(
            name=name,
            type=column_type,
            # `nullable`, not `null`. The latter reads better and
            # matches DDL, and YAML 1.1 turns it into the None key --
            # see the module note.
            nullable=bool(definition.get("nullable", True)),
            primary_key=bool(definition.get("primary_key", False)),
            length=definition.get("length"),
            precision=definition.get("precision"),
            scale=definition.get("scale"),
        )
    except ValueError as error:
        raise PackError(path, str(error)) from error


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: this module knows about dialects, because whether a silo can hold
# tables at all is decided by whether a dialect exists for its kind. Listing the
# relational kinds again here is how the two lists drift.
#
# DEFERRED: lifecycles, seed, events and migrations are still in loader.py. They
# come out one at a time, each verified. Events is the large one -- 638 lines --
# and wants taking in pieces of its own rather than in a single move.
