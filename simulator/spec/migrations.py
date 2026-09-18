"""
migrations.py  (a pack declaring when its own schema drifts)

The vocabulary a pack uses to say "on day forty, add a column", and
the same vocabulary the interactive console uses to say "add one now"
-- build_change is shared by both, because a console with its own
words for the same operations would be two vocabularies to keep in
step and the second would drift.

THE TIMELINE IS VALIDATED AGAINST ITSELF, which is the reason this is
more than a lookup table. Each migration is applied in order to a COPY
of the declared schema, so the next is checked against the shape the
previous one left. A pack that drops a column twice, or renames one
and then refers to the old name, fails when the file is read rather
than on day ninety of a run.

MIGRATION_OPERATIONS is explicit rather than derived from the class
names, so the words a pack writes are a decision rather than an
accident of refactoring.
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from simulator.drift import (
    AddColumn,
    AddTable,
    ChangeColumnType,
    DropColumn,
    DropTable,
    RenameColumn,
    RenameTable,
    RescaleColumn,
)
from simulator.schema import Column, Schema
from simulator.spec.model import Migration
from simulator.spec.schemas import NUMERIC_TYPES, _load_column, _load_table
from simulator.spec.values import (
    PackError,
    _duration,
    _require_mapping,
    _string,
)


def _load_migrations(raw: Any, schemas: dict[str, Schema]) -> tuple[Migration, ...]:
    """Parse the timeline, and check it against itself.

    Each migration is applied in order to a copy of the declared
    schema, so the next one is validated against the shape the
    previous one left behind. A pack that drops a column twice, or
    renames a column and then refers to the old name, fails when the
    file is read rather than on day ninety of a run.
    """
    if not isinstance(raw, list):
        raise PackError("migrations", "must be a list")

    working = dict(schemas)
    migrations = []
    for index, definition in enumerate(raw):
        path = f"migrations[{index}]"
        definition = _require_mapping(definition, path)
        unknown = sorted(set(definition) - _MIGRATION_KEYS)
        if unknown:
            raise PackError(path, f"does not understand {unknown}")

        at_seconds = _duration(definition.get("at", 0), f"{path}.at")
        if at_seconds <= 0:
            raise PackError(
                f"{path}.at",
                "must be a positive interval from the start of the run; a migration at "
                "zero would be indistinguishable from the schema itself"
            )
        operation = _string(definition, "operation", path)
        if operation not in MIGRATION_OPERATIONS:
            raise PackError(
                path,
                f"unknown operation {operation!r}; available: "
                f"{sorted(MIGRATION_OPERATIONS)}"
            )

        silo_name, table_name = _migration_target(definition, path, working)
        change = MIGRATION_OPERATIONS[operation](definition, path, table_name,
                                                  working[silo_name])
        try:
            working[silo_name] = change.revise(working[silo_name])
        except (KeyError, ValueError) as error:
            raise PackError(path, str(error)) from error
        migrations.append(Migration(silo=silo_name, at_seconds=at_seconds, change=change))

    # Sorted, so the runner can apply them in order without caring what
    # order the file listed them in.
    return tuple(sorted(migrations, key=lambda migration: migration.at_seconds))

_MIGRATION_KEYS = frozenset({"at", "operation", "table", "column", "to", "type",
                             "length", "precision", "scale", "nullable", "factor",
                             "columns"})

def _migration_target(definition: dict, path: str,
                      schemas: dict[str, Schema]) -> tuple[str, str]:
    qualified = _string(definition, "table", path)
    if qualified.count(".") != 1:
        raise PackError(path, f"table {qualified!r} must be written as silo.table")
    silo_name, table_name = qualified.split(".")
    if silo_name not in schemas:
        raise PackError(path, f"no schema is declared for silo {silo_name!r}")
    return silo_name, table_name

def _migration_column(definition: dict, path: str, name: str) -> Column:
    """Build a column from the migration's own type keys."""
    return _load_column(name, {key: definition[key]
                               for key in ("type", "length", "precision", "scale",
                                           "nullable")
                               if key in definition}, path)

def _add_column(definition, path, table_name, schema):
    name = _string(definition, "column", path)
    return AddColumn(table=table_name, column=_migration_column(definition, path, name))

def _drop_column(definition, path, table_name, schema):
    return DropColumn(table=table_name, column=_string(definition, "column", path))

def _rename_column(definition, path, table_name, schema):
    return RenameColumn(table=table_name, old_name=_string(definition, "column", path),
                        new_name=_string(definition, "to", path))

def _change_column_type(definition, path, table_name, schema):
    name = _string(definition, "column", path)
    return ChangeColumnType(table=table_name, column=name,
                            new_column=_migration_column(definition, path, name))

def _rename_table(definition, path, table_name, schema):
    return RenameTable(old_name=table_name, new_name=_string(definition, "to", path))

def _drop_table(definition, path, table_name, schema):
    return DropTable(table=table_name)

def _add_table(definition, path, table_name, schema):
    columns = definition.get("columns")
    if not isinstance(columns, dict) or not columns:
        raise PackError(path, "adding a table needs its `columns`")
    return AddTable(table=_load_table(table_name, {"columns": columns}, path))

def _rescale_column(definition, path, table_name, schema):
    # Checked here rather than through revise(), which for a rescale is
    # a no-op -- it changes no structure, so it has nothing to revise
    # and would validate nothing. Found by a test expecting a rename to
    # invalidate a later rescale of the old name, which it did not.
    name = _string(definition, "column", path)
    try:
        column = schema.table(table_name).column(name)
    except KeyError as error:
        raise PackError(path, str(error)) from error
    if column.type not in NUMERIC_TYPES:
        raise PackError(
            path,
            f"{table_name}.{name} is {column.type.value}, which cannot be rescaled; "
            f"rescalable types are {sorted(t.value for t in NUMERIC_TYPES)}"
        )
    if "factor" not in definition:
        raise PackError(path, "rescaling needs a `factor`")
    try:
        factor = Decimal(str(definition["factor"]))
    except InvalidOperation as error:
        raise PackError(path, f"factor {definition['factor']!r} is not a number") from error
    if factor == 0:
        raise PackError(path, "a factor of zero would erase the column, not rescale it")
    return RescaleColumn(table=table_name, column=name, factor=factor)

MIGRATION_OPERATIONS = {
    "add_column": _add_column,
    "drop_column": _drop_column,
    "rename_column": _rename_column,
    "change_column_type": _change_column_type,
    "add_table": _add_table,
    "drop_table": _drop_table,
    "rename_table": _rename_table,
    "rescale_column": _rescale_column,
}

def build_change(operation: str, fields: dict, schema: Schema, path: str = "drift"):
    """One drift operation, from a pack's own vocabulary.

    Shared by the migration loader and the interactive console, so
    `drift add_column table=... column=...` typed at a prompt means
    exactly what `operation: add_column` means in a pack file.
    """
    if operation not in MIGRATION_OPERATIONS:
        raise PackError(
            path, f"unknown operation {operation!r}; available: {sorted(MIGRATION_OPERATIONS)}"
        )
    qualified = _string(fields, "table", path)
    if qualified.count(".") != 1:
        raise PackError(path, f"table {qualified!r} must be written as silo.table")
    _, table_name = qualified.split(".")
    return MIGRATION_OPERATIONS[operation](fields, path, table_name, schema)
