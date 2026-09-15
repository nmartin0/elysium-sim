"""
drift.py  (changing a live database's shape while a consumer reads it)

THE REASON THIS PROJECT EXISTS. A simulator that only changes rows is
a fixture generator. One that adds, drops, renames and retypes columns
underneath a connected consumer is testing something no static fixture
reaches, because the interesting failures are not "the data was
wrong" -- they are "the schema moved and nobody noticed".

WHY THIS IS A CLASS HIERARCHY. Eight operations share one genuine
contract -- apply yourself to a database, say what you did, declare
whether you are breaking -- and differ entirely in how, from a
one-line ALTER to a statement each engine spells differently. The base
class also carries the part that must not vary: verify the result
against the engine's own catalogue, then record it. A bag of functions
would repeat that epilogue eight times and one copy would drift.

is_breaking FOLLOWS PALANTIR'S TAXONOMY rather than a judgement made
here. Their documentation states that additive changes to a backing
dataset do not interfere with synchronization, while destructive ones
are refused by default and need the property unmapped and the table
re-registered by hand. Their named errors map onto these operations
directly -- FoundryColumnNameNotFound when a column backing a property
is removed -- and their breaking list is property type change, backing
datasource change, primary key change.

WHAT "BREAKING" MEANS HERE. Not "this will raise". It means a consumer
that mapped the old shape now holds a mapping that no longer matches,
so the pass condition is that it SAYS SO rather than crashing or,
worse, quietly returning wrong answers. Additive changes carry the
opposite expectation: nothing downstream should notice.

RescaleColumn is the sharp one. It changes no structure at all --
every read succeeds, every type still checks -- and the answers are
now wrong unless the consumer noticed. Every other operation
eventually throws something somewhere; this one returns confident
wrong answers, which makes its pass condition "the numbers are still
right" rather than "it failed well".
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from simulator.dialect import dialect_for
from simulator.relational import catalogue_columns
from simulator.schema import Column, Schema, Table
from simulator.silo import Silo, SiloError

#: Recorded inside the simulated database, which is realism rather
#: than leakage: real business databases carry exactly this. Flyway
#: writes flyway_schema_history, Alembic writes alembic_version. A
#: consumer meeting a table it has no mapping for is a thing that
#: genuinely happens, and it is the mildest form of the additive drift
#: this module exists to exercise.
HISTORY_TABLE = "_simulator_migrations"

_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
  applied_at  {timestamp} NOT NULL,
  operation   {text} NOT NULL,
  detail      {text} NOT NULL,
  breaking    {integer} NOT NULL
)
"""


class DriftError(Exception):
    """A schema change could not be applied, or did not take effect."""


class SchemaChange(ABC):
    """One structural change to a live database."""

    #: Whether a consumer holding the previous shape is invalidated.
    is_breaking: ClassVar[bool] = False

    @abstractmethod
    def statements(self, silo: Silo, schema: Schema) -> list[tuple[str, list]]:
        """The SQL to run, with its bound parameters."""

    @abstractmethod
    def revise(self, schema: Schema) -> Schema:
        """What the schema becomes."""

    @abstractmethod
    def describe(self) -> str:
        """A one-line operator-facing account of what changed."""

    def apply(self, silo: Silo, database: str, schema: Schema,
              at: datetime) -> Schema:
        """Run the change, prove it took effect, and record it.

        The verification is not ceremony. An operation that revised the
        schema object and left the database untouched would satisfy
        every assertion made against that object, because the object is
        the thing it edited. Comparing against the engine's own
        catalogue is the only check that can catch it, and it runs on
        every operation rather than only the risky one -- because which
        one is risky is exactly what a future change might get wrong.
        """
        revised = self.revise(schema)
        with silo.connect(database) as connection:  # type: ignore[attr-defined]
            with connection.cursor() as cursor:
                for statement, parameters in self.statements(silo, schema):
                    cursor.execute(statement, parameters)
            connection.commit()

        _verify(silo, database, revised)
        _record(silo, database, at, type(self).__name__, self.describe(), self.is_breaking)
        return revised


# -- additive ---------------------------------------------------------

@dataclass(frozen=True)
class AddColumn(SchemaChange):
    """Add a column. The mildest drift there is, and safe by design."""

    is_breaking: ClassVar[bool] = False

    table: str
    column: Column

    def statements(self, silo: Silo, schema: Schema) -> list[tuple[str, list]]:
        if self.column.primary_key:
            raise DriftError("cannot add a primary key column to an existing table")
        if not self.column.nullable:
            # Both engines refuse this without a default, and rightly:
            # existing rows would have no value. Raising here names the
            # real reason rather than passing through the engine's
            # terser complaint from two layers down.
            raise DriftError(
                f"cannot add NOT NULL column {self.column.name!r} to a populated "
                f"table without a default; declare it nullable"
            )
        return [(dialect_for(silo.kind).add_column(self.table, self.column), [])]

    def revise(self, schema: Schema) -> Schema:
        table = schema.table(self.table)
        return _replacing(schema, Table(name=table.name,
                                        columns=(*table.columns, self.column)))

    def describe(self) -> str:
        return f"added {self.table}.{self.column.name} ({self.column.type.value})"


@dataclass(frozen=True)
class AddTable(SchemaChange):
    """Add a table. Additive, and invisible to a consumer."""

    is_breaking: ClassVar[bool] = False

    table: Table

    def statements(self, silo: Silo, schema: Schema) -> list[tuple[str, list]]:
        return [(dialect_for(silo.kind).create_table(self.table), [])]

    def revise(self, schema: Schema) -> Schema:
        if schema.has_table(self.table.name):
            raise DriftError(f"schema already has a table {self.table.name!r}")
        return Schema(tables=(*schema.tables, self.table))

    def describe(self) -> str:
        return f"added table {self.table.name}"


# -- destructive ------------------------------------------------------

@dataclass(frozen=True)
class DropColumn(SchemaChange):
    """Remove a column. Foundry's FoundryColumnNameNotFound case."""

    is_breaking: ClassVar[bool] = True

    table: str
    column: str

    def statements(self, silo: Silo, schema: Schema) -> list[tuple[str, list]]:
        if schema.table(self.table).column(self.column).primary_key:
            raise DriftError(f"cannot drop {self.table}.{self.column}: it is the primary key")
        return [(dialect_for(silo.kind).drop_column(self.table, self.column), [])]

    def revise(self, schema: Schema) -> Schema:
        table = schema.table(self.table)
        table.column(self.column)
        return _replacing(schema, Table(
            name=table.name,
            columns=tuple(c for c in table.columns if c.name != self.column),
        ))

    def describe(self) -> str:
        return f"dropped {self.table}.{self.column}"


@dataclass(frozen=True)
class RenameColumn(SchemaChange):
    """Rename a column, leaving its data in place.

    Nastier than a drop from a consumer's point of view: every value
    is still there, so the failure reads as a missing column while the
    data sits untouched one name away.
    """

    is_breaking: ClassVar[bool] = True

    table: str
    old_name: str
    new_name: str

    def statements(self, silo: Silo, schema: Schema) -> list[tuple[str, list]]:
        schema.table(self.table).column(self.old_name)
        return [(dialect_for(silo.kind).rename_column(
            self.table, self.old_name, self.new_name), [])]

    def revise(self, schema: Schema) -> Schema:
        table = schema.table(self.table)
        existing = table.column(self.old_name)
        renamed = Column(name=self.new_name, type=existing.type,
                         nullable=existing.nullable, primary_key=existing.primary_key,
                         length=existing.length, precision=existing.precision,
                         scale=existing.scale)
        # Position preserved rather than dropped and re-appended:
        # column ORDER is observable through SELECT *, so a rename that
        # reordered would itself be an unintended schema change.
        return _replacing(schema, Table(
            name=table.name,
            columns=tuple(renamed if c.name == self.old_name else c
                          for c in table.columns),
        ))

    def describe(self) -> str:
        return f"renamed {self.table}.{self.old_name} to {self.new_name}"


@dataclass(frozen=True)
class ChangeColumnType(SchemaChange):
    """Retype a column, keeping what is already in it.

    On Foundry's own breaking-change list as "changing the data type
    of an existing property", and the operation the two engines spell
    most differently -- see dialect.py.
    """

    is_breaking: ClassVar[bool] = True

    table: str
    column: str
    new_column: Column

    def statements(self, silo: Silo, schema: Schema) -> list[tuple[str, list]]:
        existing = schema.table(self.table).column(self.column)
        if self.new_column.name != existing.name:
            raise DriftError(
                f"retyping {self.table}.{self.column} cannot also rename it to "
                f"{self.new_column.name!r}; use RenameColumn for that"
            )
        return [(dialect_for(silo.kind).change_column_type(self.table, self.new_column), [])]

    def revise(self, schema: Schema) -> Schema:
        table = schema.table(self.table)
        table.column(self.column)
        return _replacing(schema, Table(
            name=table.name,
            columns=tuple(self.new_column if c.name == self.column else c
                          for c in table.columns),
        ))

    def describe(self) -> str:
        return f"retyped {self.table}.{self.column} to {self.new_column.type.value}"


@dataclass(frozen=True)
class RenameTable(SchemaChange):
    """Rename a table, leaving its rows in place."""

    is_breaking: ClassVar[bool] = True

    old_name: str
    new_name: str

    def statements(self, silo: Silo, schema: Schema) -> list[tuple[str, list]]:
        schema.table(self.old_name)
        return [(dialect_for(silo.kind).rename_table(self.old_name, self.new_name), [])]

    def revise(self, schema: Schema) -> Schema:
        table = schema.table(self.old_name)
        renamed = Table(name=self.new_name, columns=table.columns)
        return Schema(tables=tuple(renamed if t.name == self.old_name else t
                                   for t in schema.tables))

    def describe(self) -> str:
        return f"renamed table {self.old_name} to {self.new_name}"


@dataclass(frozen=True)
class DropTable(SchemaChange):
    """Remove a table, and everything in it."""

    is_breaking: ClassVar[bool] = True

    table: str

    def statements(self, silo: Silo, schema: Schema) -> list[tuple[str, list]]:
        schema.table(self.table)
        return [(dialect_for(silo.kind).drop_table(self.table), [])]

    def revise(self, schema: Schema) -> Schema:
        schema.table(self.table)
        return Schema(tables=tuple(t for t in schema.tables if t.name != self.table))

    def describe(self) -> str:
        return f"dropped table {self.table}"


# -- semantic ---------------------------------------------------------

@dataclass(frozen=True)
class RescaleColumn(SchemaChange):
    """Multiply every value in a column by a constant.

    THE ONE THAT CHANGES NO STRUCTURE AT ALL. The schema is untouched,
    every read succeeds, every type still checks -- and the meaning has
    moved. Amounts recorded in dollars yesterday and cents today is a
    real migration that real systems perform, and it is the cheapest
    way to produce the failure class that matters most: not an error,
    but a confident wrong answer.

    Marked breaking even though nothing downstream will raise, because
    "breaking" here means a consumer's understanding is now invalid --
    which it is. A consumer that NOTICES this one is doing much better
    than one that merely survives a dropped column.
    """

    is_breaking: ClassVar[bool] = True

    table: str
    column: str
    factor: Decimal

    def statements(self, silo: Silo, schema: Schema) -> list[tuple[str, list]]:
        schema.table(self.table).column(self.column)
        return [(dialect_for(silo.kind).rescale_column(self.table, self.column),
                 [self.factor])]

    def revise(self, schema: Schema) -> Schema:
        return schema

    def describe(self) -> str:
        return f"rescaled {self.table}.{self.column} by {self.factor}"


# -- shared machinery -------------------------------------------------

def _replacing(schema: Schema, table: Table) -> Schema:
    schema.table(table.name)
    return Schema(tables=tuple(table if t.name == table.name else t
                               for t in schema.tables))


def _verify(silo: Silo, database: str, schema: Schema) -> None:
    """Raise unless the engine's catalogue matches the revised schema."""
    for table in schema.tables:
        actual = catalogue_columns(silo, database, table.name)
        expected = [column.name for column in table.columns]
        if actual != expected:
            raise DriftError(
                f"{silo.name}.{database}: table {table.name!r} did not change as "
                f"declared -- the engine reports {actual}, the schema now says "
                f"{expected}"
            )


def _record(silo: Silo, database: str, at: datetime, operation: str,
            detail: str, breaking: bool) -> None:
    """Write the change to the history table, creating it on first use.

    Attribution is the whole point. Without it, a consumer breaking at
    14:05 has to be correlated against whatever the simulator happened
    to be doing; with it, the answer is one query.
    """
    dialect = dialect_for(silo.kind)
    from simulator.schema import ColumnType

    ddl = _HISTORY_DDL.format(
        table=dialect.quote(HISTORY_TABLE),
        timestamp=dialect.render_type(Column("applied_at", ColumnType.TIMESTAMP)),
        text=dialect.render_type(Column("detail", ColumnType.TEXT, length=500)),
        integer=dialect.render_type(Column("breaking", ColumnType.INTEGER)),
    )
    insert = (f"INSERT INTO {dialect.quote(HISTORY_TABLE)} "
              f"(applied_at, operation, detail, breaking) VALUES "
              f"({dialect.placeholder}, {dialect.placeholder}, "
              f"{dialect.placeholder}, {dialect.placeholder})")
    with silo.connect(database) as connection:  # type: ignore[attr-defined]
        with connection.cursor() as cursor:
            cursor.execute(ddl)
            cursor.execute(insert, [at, operation, detail, int(breaking)])
        connection.commit()


def history(silo: Silo, database: str) -> list[dict[str, Any]]:
    """Every schema change applied to this silo, oldest first."""
    from simulator.relational import fetch_all

    dialect = dialect_for(silo.kind)
    try:
        rows = fetch_all(
            silo, database,
            f"SELECT applied_at, operation, detail, breaking "
            f"FROM {dialect.quote(HISTORY_TABLE)} ORDER BY applied_at, operation",
        )
    except silo.driver_errors() as error:
        # No history table, which means nothing has drifted. Narrowed
        # to the driver's own errors so that a bug in the query -- or
        # in the dialect that built it -- surfaces as itself rather
        # than as "nothing has drifted yet", which is a plausible
        # answer and therefore the worst possible disguise.
        raise SiloError(
            f"{silo.name}.{database} has no migration history; nothing has drifted yet"
        ) from error
    return [
        {"applied_at": row[0], "operation": row[1], "detail": row[2],
         "breaking": bool(row[3])}
        for row in rows
    ]


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: apply() verifies against the engine's catalogue on EVERY operation,
# not just the risky ones. An operation that revised the schema object and left
# the database untouched would satisfy any assertion made against that object,
# because the object is what it edited.
#
# RESOLVED: is_breaking follows Palantir's taxonomy rather than a local
# judgement -- additive backing-dataset changes do not interfere with
# synchronization; property type change, backing datasource change and primary
# key change are breaking. RescaleColumn is the deliberate extension: breaking
# in the sense that matters (a consumer's understanding is now wrong) while
# raising nothing anywhere.
#
# RESOLVED: the history table lives INSIDE the simulated database. That is
# realism, not leakage -- Flyway and Alembic both do exactly this, and a
# consumer meeting an unmapped table is itself the mildest additive drift.
#
# DEFERRED (known, intentional, not yet built): no AddDatabase or DropDatabase.
# Both are silo operations rather than DDL, and both need a decision this
# module does not have: a consumer that reads its configuration once at startup
# cannot see a silo appear or vanish until it restarts. Whether the answer is
# "restart the consumer" or "the consumer should reload" is a conversation, and
# the operations are shaped by which it is.
#
# DEFERRED: no packs declare drift. A migration timeline in a pack file -- "on
# day 40, add customers.loyalty_tier" -- is the natural next step, and it should
# be designed against these operations now that they exist rather than guessed
# at beforehand.
#
# DEFERRED: no RepurposeColumn -- rename a column and add a new one under the
# old name holding different data. The sharpest semantic drift there is: every
# read succeeds, every value is wrong, nothing fires. Composable from
# RenameColumn plus AddColumn today; worth its own operation once a scenario
# layer exists to fire it deliberately.
