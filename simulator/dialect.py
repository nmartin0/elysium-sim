"""
dialect.py  (rendering a neutral schema as one engine's SQL)

WHY THIS EXISTS NOW AND NOT BEFORE. An earlier note in this project
said plainly that a dialect abstraction with one implementation would
be the speculative abstraction its own rules forbid, and that the seam
should be cut when a second engine arrived. It has. Two engines now
render the same declarations and disagree about almost every detail,
which is the "something real to factor out" that the silos package
docstring was waiting for.

WHERE THEY DISAGREE, and none of it is cosmetic:

  - IDENTIFIER QUOTING. PostgreSQL uses double quotes; MariaDB uses
    backticks, and its double quotes mean a string literal unless
    ANSI_QUOTES is set. The same DDL sent to the wrong engine is not
    an error, it is a table with a column whose name is a string.
  - TEXT. PostgreSQL's TEXT is unbounded and idiomatic. MariaDB has
    VARCHAR(n) and TEXT, and they are genuinely different: VARCHAR can
    be indexed whole, TEXT needs a prefix length. A declared length
    should become VARCHAR there and is simply ignored by PostgreSQL.
  - BOOLEAN. PostgreSQL has a real one. MariaDB's BOOLEAN is an alias
    for TINYINT(1), so a consumer reads 0 and 1 rather than false and
    true -- a difference worth preserving rather than papering over,
    because a consumer of the real thing would meet it.
  - TIMESTAMPS. PostgreSQL's TIMESTAMPTZ keeps the offset. MariaDB's
    DATETIME does not, and its TIMESTAMP silently converts through the
    session time zone and has a 2038 limit. DATETIME is the honest
    choice there, and the lost offset is a real property of the
    system, not a defect in the rendering.

THE RENDERING IS PURE. Nothing here connects to anything. A dialect
turns declarations into strings; the silos execute them. That is what
lets every statement in this file be tested without a server, and then
separately proved valid by executing it against a real one.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import ClassVar

from simulator.schema import Column, ColumnType, Table


class SqlDialect(ABC):
    """How one engine spells a schema."""

    #: Matches the silo kind this dialect belongs to.
    kind: ClassVar[str]

    #: The character pair used to quote an identifier.
    quote_character: ClassVar[str]

    #: The placeholder a parameterised statement uses. psycopg and
    #: pymysql both accept %s; it is a property of the driver rather
    #: than the engine, which is why it is stated rather than assumed.
    placeholder: ClassVar[str] = "%s"

    def quote(self, identifier: str) -> str:
        """Quote an identifier.

        Identifiers reaching here are already restricted to plain
        Python identifiers by schema.py's validation, so there is
        nothing to escape -- but they are quoted anyway, because a
        column legitimately named `order` or `group` is a reserved word
        on both engines and would otherwise be a syntax error a pack
        author could not diagnose.
        """
        return f"{self.quote_character}{identifier}{self.quote_character}"

    @abstractmethod
    def render_type(self, column: Column) -> str:
        """This engine's spelling of a column's declared type."""

    def render_column(self, column: Column) -> str:
        parts = [self.quote(column.name), self.render_type(column)]
        if column.primary_key:
            parts.append("PRIMARY KEY")
        elif not column.nullable:
            parts.append("NOT NULL")
        return " ".join(parts)

    def create_table(self, table: Table) -> str:
        body = ",\n  ".join(self.render_column(column) for column in table.columns)
        return f"CREATE TABLE {self.quote(table.name)} (\n  {body}\n)"

    def insert(self, table_name: str, columns: Sequence[str]) -> str:
        """A parameterised INSERT. Values are bound, never interpolated."""
        if not columns:
            raise ValueError(f"cannot insert into {table_name!r} with no columns")
        names = ", ".join(self.quote(name) for name in columns)
        placeholders = ", ".join(self.placeholder for _ in columns)
        return f"INSERT INTO {self.quote(table_name)} ({names}) VALUES ({placeholders})"

    @abstractmethod
    def create_database(self, name: str) -> str:
        """Create a database inside this engine's instance."""

    # -- changing a table that already exists ------------------------
    #
    # The engines agree about adding, dropping and renaming, and
    # disagree completely about retyping -- which is the one that
    # matters most, since it is on Foundry's own breaking-change list.

    def add_column(self, table_name: str, column: Column) -> str:
        return (f"ALTER TABLE {self.quote(table_name)} "
                f"ADD COLUMN {self.render_column(column)}")

    def drop_column(self, table_name: str, column_name: str) -> str:
        return (f"ALTER TABLE {self.quote(table_name)} "
                f"DROP COLUMN {self.quote(column_name)}")

    def rename_column(self, table_name: str, old_name: str, new_name: str) -> str:
        return (f"ALTER TABLE {self.quote(table_name)} "
                f"RENAME COLUMN {self.quote(old_name)} TO {self.quote(new_name)}")

    def rename_table(self, old_name: str, new_name: str) -> str:
        return f"ALTER TABLE {self.quote(old_name)} RENAME TO {self.quote(new_name)}"

    def drop_table(self, table_name: str) -> str:
        return f"DROP TABLE {self.quote(table_name)}"

    @abstractmethod
    def change_column_type(self, table_name: str, column: Column) -> str:
        """Retype an existing column, keeping what is already in it."""

    def rescale_column(self, table_name: str, column_name: str) -> str:
        """Multiply every value in a column by a bound parameter.

        The one change that alters no structure at all -- every read
        still succeeds, every type still checks, and the meaning has
        moved. Dollars becoming cents is a real migration that real
        systems perform.
        """
        quoted = self.quote(column_name)
        return (f"UPDATE {self.quote(table_name)} SET {quoted} = {quoted} * "
                f"{self.placeholder} WHERE {quoted} IS NOT NULL")


class PostgresDialect(SqlDialect):
    kind: ClassVar[str] = "postgresql"
    quote_character: ClassVar[str] = '"'

    def render_type(self, column: Column) -> str:
        match column.type:
            case ColumnType.TEXT:
                # Length is deliberately ignored. PostgreSQL's TEXT is
                # unbounded, has no performance cost against VARCHAR(n),
                # and is what its own documentation recommends; honouring
                # a length here would add a constraint the real system
                # would not have.
                return "TEXT"
            case ColumnType.INTEGER:
                return "INTEGER"
            case ColumnType.BIGINT:
                return "BIGINT"
            case ColumnType.DECIMAL:
                return f"NUMERIC({column.precision}, {column.scale})"
            case ColumnType.BOOLEAN:
                return "BOOLEAN"
            case ColumnType.DATE:
                return "DATE"
            case ColumnType.TIMESTAMP:
                # With the offset kept, which is the whole reason to
                # prefer it over TIMESTAMP WITHOUT TIME ZONE.
                return "TIMESTAMPTZ"
        raise ValueError(f"unhandled column type {column.type}")

    def create_database(self, name: str) -> str:
        return f"CREATE DATABASE {self.quote(name)}"

    def change_column_type(self, table_name: str, column: Column) -> str:
        # USING is not optional here. PostgreSQL will not implicitly
        # convert between most types, so a plain ALTER ... TYPE fails
        # with "column cannot be cast automatically" -- and the whole
        # point of retyping in a drift test is that the data already
        # in the column comes along.
        rendered = self.render_type(column)
        return (f"ALTER TABLE {self.quote(table_name)} "
                f"ALTER COLUMN {self.quote(column.name)} TYPE {rendered} "
                f"USING {self.quote(column.name)}::{rendered}")


class MariaDbDialect(SqlDialect):
    kind: ClassVar[str] = "mariadb"
    quote_character: ClassVar[str] = "`"

    def render_type(self, column: Column) -> str:
        match column.type:
            case ColumnType.TEXT:
                # A declared length becomes VARCHAR, which can be
                # indexed whole; without one, TEXT, which cannot. That
                # difference is real on this engine and is why the
                # neutral declaration carries an optional length at all.
                return f"VARCHAR({column.length})" if column.length else "TEXT"
            case ColumnType.INTEGER:
                return "INT"
            case ColumnType.BIGINT:
                return "BIGINT"
            case ColumnType.DECIMAL:
                return f"DECIMAL({column.precision}, {column.scale})"
            case ColumnType.BOOLEAN:
                # An alias for TINYINT(1) here, so a consumer reads 0
                # and 1 rather than false and true. Preserved rather
                # than papered over: a consumer of the real thing meets
                # exactly this.
                return "BOOLEAN"
            case ColumnType.DATE:
                return "DATE"
            case ColumnType.TIMESTAMP:
                # DATETIME rather than TIMESTAMP. MariaDB's TIMESTAMP
                # converts through the session time zone on the way in
                # and out, and runs out in 2038. DATETIME does neither,
                # and the offset it cannot keep is a real property of
                # the system rather than a defect here.
                return "DATETIME"
        raise ValueError(f"unhandled column type {column.type}")

    def create_table(self, table: Table) -> str:
        # InnoDB and utf8mb4 stated explicitly. Defaults vary by server
        # version and configuration, and the combination that bites is
        # a table created as utf8 (three bytes) which then rejects an
        # emoji in a customer's name -- a genuinely common production
        # failure on this engine.
        return super().create_table(table) + " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"

    def create_database(self, name: str) -> str:
        return f"CREATE DATABASE {self.quote(name)} CHARACTER SET utf8mb4"

    def change_column_type(self, table_name: str, column: Column) -> str:
        # MODIFY COLUMN restates the WHOLE definition, so nullability
        # has to be repeated or it is silently dropped -- a NOT NULL
        # column quietly becoming nullable is a change nobody asked
        # for, arriving inside a change they did.
        definition = f"{self.quote(column.name)} {self.render_type(column)}"
        if not column.nullable and not column.primary_key:
            definition += " NOT NULL"
        return f"ALTER TABLE {self.quote(table_name)} MODIFY COLUMN {definition}"


#: By silo kind, so a caller with a silo can find its dialect without
#: a conditional. Explicit rather than discovered: two entries do not
#: need a registry mechanism, and a greppable dict is what a reader
#: wants when a kind has no dialect.
DIALECTS: dict[str, SqlDialect] = {
    PostgresDialect.kind: PostgresDialect(),
    MariaDbDialect.kind: MariaDbDialect(),
}


def dialect_for(kind: str) -> SqlDialect:
    if kind not in DIALECTS:
        raise KeyError(
            f"no SQL dialect for silo kind {kind!r}; relational kinds are {sorted(DIALECTS)}"
        )
    return DIALECTS[kind]


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): this module exists because a SECOND engine now
# does. An earlier note said a dialect abstraction with one implementation
# would be speculative and that the seam should be cut when a second arrived.
# The silos package docstring said each module would own its own SQL "until
# there is something real to factor out"; two engines rendering the same
# declarations is that, and the docstring has been updated rather than left
# contradicting this file.
#
# RESOLVED: identifiers are quoted even though schema.py already restricts them
# to plain Python identifiers, so there is nothing to escape. A column named
# `order` or `group` is a reserved word on both engines and would otherwise be
# a syntax error a pack author could not diagnose from the message.
#
# RESOLVED: MariaDB gets DATETIME rather than TIMESTAMP, and the lost offset is
# deliberate. TIMESTAMP converts through the session time zone in both
# directions and expires in 2038; DATETIME is the honest rendering, and a
# consumer meeting a timestamp without an offset is meeting the real system.
#
# RESOLVED: a drop_table() renderer was written here and deleted before commit
# -- nothing called it, and the drift operations that will are not in this
# repository yet.
#
# DEFERRED (known, intentional, not yet built): no SQLite dialect, even though
# SqliteSilo exists. SQLite's dynamic typing means the same declarations behave
# differently enough that it is a design question rather than a third case
# statement -- a column declared DECIMAL holds whatever is put in it. Worth
# doing; not worth doing by analogy.
#
# RESOLVED: ALTER rendering exists, and the engines diverge exactly where
# predicted. PostgreSQL needs ALTER ... TYPE ... USING, because it refuses to
# convert between most types implicitly. MariaDB needs MODIFY COLUMN with the
# whole definition restated, which means nullability has to be repeated or a
# NOT NULL column silently becomes nullable inside a change nobody asked for.
