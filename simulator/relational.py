"""
relational.py  (creating databases, applying schemas, writing rows)

The layer that turns declarations into something a consumer can read.
schema.py says what a table is, dialect.py says how an engine spells
it, the silo modules run the server -- and this puts the three
together.

Written against DB-API, not against a driver. psycopg and PyMySQL
disagree about plenty, but both implement PEP 249: `cursor()`,
`execute()`, `executemany()`, `commit()`. Everything here uses only
that, which is why there is no PostgresStore and MariaDbStore pair.
The engine-specific parts are already handled where they belong --
connection arguments by each silo, SQL text by each dialect -- and a
third parallel hierarchy would just be a place for them to be handled
again, differently.

WHERE the engines genuinely still differ, and it is one thing:
PostgreSQL refuses CREATE DATABASE inside a transaction block, so
create_database() asks for autocommit. MariaDB does not care. Asking
for it unconditionally is correct on both and saves a conditional.

Verification reads information_schema, which both engines have. That
is worth more than it sounds. The alternative is trusting that the
DDL did what the declaration said, and a schema layer cannot check
itself: an applier that quietly skipped a column would satisfy every
assertion made against the Schema object, because the Schema object is
what it was reading. Comparing against the catalogue is the only check
that can fail.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from simulator.dialect import dialect_for
from simulator.schema import Column, ColumnType, Schema, Table
from simulator.silo import Silo, SiloError


def create_database(silo: Silo, name: str, seed: int = 1) -> None:
    """Create one database inside a relational silo's instance."""
    dialect = dialect_for(silo.kind)
    with silo.connect(autocommit=True) as connection:  # type: ignore[attr-defined]
        with connection.cursor() as cursor:
            cursor.execute(dialect.create_database(name))
    # Immediately, so a database never exists without its reader. On
    # PostgreSQL the ordering is load-bearing as well as tidy: default
    # privileges apply only to tables created after they are granted.
    grant_read_only(silo, name, seed)


def grant_read_only(silo: Silo, database: str, seed: int = 1) -> None:
    """Give this silo's reader account SELECT on a database, and no more.

    Called as part of creating one, so the grant exists before any
    schema does -- which PostgreSQL requires, because its default
    privileges affect only tables created afterwards.
    """
    from simulator.silos.reader import provision_mariadb, provision_postgres

    if silo.kind == "postgresql":
        provision_postgres(silo, database, silo.superuser, seed)  # type: ignore[attr-defined]
    elif silo.kind == "mariadb":
        provision_mariadb(silo, database, seed)


def truncate(silo: Silo, database: str, table: Table) -> None:
    """Empty one table, for a replica about to be refilled.

    DELETE rather than TRUNCATE, because TRUNCATE is DDL on both
    engines: MariaDB commits the open transaction around it, which
    would break the tick's atomicity, and PostgreSQL takes a lock that
    a concurrent reader waits behind. A reporting copy being rebuilt
    should not stop somebody reading it.
    """
    dialect = dialect_for(silo.kind)
    with silo.connect(database) as connection:  # type: ignore[attr-defined]
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {dialect.quote(table.name)}")


def apply_schema(silo: Silo, database: str, schema: Schema) -> None:
    """Create every table in a schema, in declared order.

    Order is preserved rather than sorted, because a schema author
    writes parents before children and will expect foreign keys to work
    that way when they arrive. Sorting alphabetically now would build a
    habit that breaks later.
    """
    dialect = dialect_for(silo.kind)
    with silo.connect(database) as connection:  # type: ignore[attr-defined]
        with connection.cursor() as cursor:
            for table in schema.tables:
                cursor.execute(dialect.create_table(table))


def insert_rows(silo: Silo, database: str, table: Table,
                rows: Sequence[Mapping[str, Any]]) -> int:
    """Insert rows into one table, in a single transaction.

    Column names come from the first row and are checked against the
    declaration before any SQL is built, so a pack's typo fails with
    the column name rather than as an engine error from inside a tick.
    Every row must carry the same keys -- a ragged batch is a bug in
    the caller, and executemany would otherwise bind the wrong values
    to the wrong columns without complaint.
    """
    if not rows:
        return 0

    dialect = dialect_for(silo.kind)
    columns = list(rows[0])
    unknown = [name for name in columns if not _has_column(table, name)]
    if unknown:
        raise SiloError(f"table {table.name!r} has no column(s) {sorted(unknown)}")

    expected = set(columns)
    for index, row in enumerate(rows):
        if set(row) != expected:
            raise SiloError(
                f"row {index} of {table.name!r} has keys {sorted(row)}, "
                f"but the first row has {sorted(expected)}; every row in a batch "
                f"must carry the same columns"
            )

    statement = dialect.insert(table.name, columns)
    values = [[row[name] for name in columns] for row in rows]
    with silo.connect(database) as connection:  # type: ignore[attr-defined]
        with connection.cursor() as cursor:
            cursor.executemany(statement, values)
    return len(rows)


def adjust_column(silo: Silo, database: str, table: Table, column: str,
                  delta: Any, where: Mapping[str, Any],
                  floor: Any | None = None) -> int:
    """Add `delta` to a numeric column on matching rows. Rows changed.

    In the DATABASE, not read-MODIFY-write. `SET quantity = quantity +
    %s` is one statement the engine applies atomically; reading the
    value, computing, and writing it back would be two round trips with
    a window between them. That window does not matter while the
    simulator is the only writer, and it will the moment a consumer
    with writeback enabled touches the same row -- which is a condition
    this tool exists to create.

    `floor` clamps with GREATEST, which both engines have. Stock cannot
    go negative, and expressing that as a clamp rather than a guard
    means it holds even when two adjustments land in the same tick.
    """
    for name in (column, *where):
        if not _has_column(table, name):
            raise SiloError(f"table {table.name!r} has no column {name!r}")
    if not where:
        # An adjustment with no predicate would silently move every row
        # in the table. A pack meaning that should say so some other
        # way; far more often it is a forgotten key.
        raise SiloError(f"adjusting {table.name}.{column} needs a `where` to match on")

    dialect = dialect_for(silo.kind)
    quoted = dialect.quote(column)
    expression = (f"{quoted} + {dialect.placeholder}" if floor is None
                  else f"GREATEST({quoted} + {dialect.placeholder}, {dialect.placeholder})")
    parameters: list[Any] = [delta] if floor is None else [delta, floor]

    predicate = " AND ".join(
        f"{dialect.quote(name)} = {dialect.placeholder}" for name in where
    )
    parameters.extend(where.values())

    statement = (f"UPDATE {dialect.quote(table.name)} SET {quoted} = {expression} "
                 f"WHERE {predicate}")
    with silo.connect(database) as connection:  # type: ignore[attr-defined]
        with connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            changed = cursor.rowcount
    return int(changed)


def update_columns(silo: Silo, database: str, table: Table,
                   values: Mapping[str, Any], where: Mapping[str, Any]) -> int:
    """Set several columns on matching rows. Rows changed.

    One statement rather than one per column: a row revised to record
    that a flight left the gate sets the actual time and the status,
    and those are one fact about the world, not two.
    """
    if not values:
        raise SiloError(f"updating {table.name} needs at least one column to set")
    for name in (*values, *where):
        if not _has_column(table, name):
            raise SiloError(f"table {table.name!r} has no column {name!r}")
    if not where:
        raise SiloError(f"updating {table.name} needs a `where` to match on")

    dialect = dialect_for(silo.kind)
    assignments = ", ".join(
        f"{dialect.quote(name)} = {dialect.placeholder}" for name in values
    )
    predicate = " AND ".join(
        f"{dialect.quote(name)} = {dialect.placeholder}" for name in where
    )
    statement = (f"UPDATE {dialect.quote(table.name)} SET {assignments} "
                 f"WHERE {predicate}")
    with silo.connect(database) as connection:  # type: ignore[attr-defined]
        with connection.cursor() as cursor:
            cursor.execute(statement, [*values.values(), *where.values()])
            return int(cursor.rowcount)


def count_rows(silo: Silo, database: str, table_name: str) -> int:
    dialect = dialect_for(silo.kind)
    with silo.connect(database) as connection:  # type: ignore[attr-defined]
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {dialect.quote(table_name)}")
            return int(cursor.fetchone()[0])


def fetch_all(silo: Silo, database: str, statement: str,
              parameters: Sequence[Any] = ()) -> list[tuple]:
    """Run a read and return its rows.

    Takes SQL rather than building it, because reading is what tests
    and scenarios do and they want to ask specific questions. Nothing
    in the simulation path reads its own silos.
    """
    with silo.connect(database) as connection:  # type: ignore[attr-defined]
        with connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return list(cursor.fetchall())


def fetch_rows_by_key(silo: Silo, database: str, table: Table, key_column: str,
                      keys: Sequence[Any]) -> dict[Any, dict]:
    """The rows for a set of keys, in one query, keyed by that column.

    One statement rather than one per key. A tick in which fifty
    entities moved would otherwise be fifty round trips to fetch rows
    the simulator is about to hand straight to an event.
    """
    if not keys:
        return {}
    dialect = dialect_for(silo.kind)
    names = [column.name for column in table.columns]
    selected = ", ".join(dialect.quote(name) for name in names)
    placeholders = ", ".join(dialect.placeholder for _ in keys)
    rows = fetch_all(
        silo, database,
        f"SELECT {selected} FROM {dialect.quote(table.name)} "
        f"WHERE {dialect.quote(key_column)} IN ({placeholders})",
        list(keys),
    )
    built = [dict(zip(names, row, strict=True)) for row in rows]
    return {row[key_column]: row for row in built}


def catalogue_columns(silo: Silo, database: str, table_name: str) -> list[str]:
    """Column names as the engine reports them, in storage order.

    information_schema, which both engines implement. Ordinal position
    rather than name order, because column order is observable through
    SELECT * and a consumer reading by position would see a change.
    """
    rows = fetch_all(
        silo, database,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (_catalogue_scope(silo, database), table_name),
    )
    return [str(row[0]) for row in rows]


def read_schema(silo: Silo, database: str) -> Schema:
    """Learn a database's shape by asking it, rather than being told.

    What lets a second process attach to a world another one is
    running. The pack file says what the schema was declared to be,
    which is no longer true once anything has drifted; the engine's own
    catalogue is the only account of what is there now.

    Types come back through the dialect's reverse mapping, so a column
    read here is safe to hand to a drift operation.

    It describes the engine, not the declaration, and those are not
    always the same. PostgreSQL stores no length for text, because its
    TEXT is unbounded and rendering one was a considered choice not to
    make; a schema read back from it therefore has length=None whatever
    the pack said. That is not a defect to paper over -- where a
    read-back and a declaration differ, the difference is information.
    """
    dialect = dialect_for(silo.kind)
    scope = _catalogue_scope(silo, database)
    keys = _primary_keys(silo, database, scope)

    tables = []
    for (table_name,) in fetch_all(
        silo, database,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name",
        (scope,),
    ):
        # The migration history is real and lives in the database on
        # purpose, but it is the simulator's bookkeeping rather than
        # part of the business, so a schema read back should not
        # suddenly contain it.
        if str(table_name).startswith("_simulator"):
            continue
        columns = tuple(
            _column_from_catalogue(
                dialect, dict(zip(dialect.catalogue_fields, row, strict=True)),
                keys.get(str(table_name)))
            for row in fetch_all(
                silo, database,
                f"SELECT {', '.join(dialect.catalogue_fields)} "
                f"FROM information_schema.columns "
                f"WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                (scope, table_name),
            )
        )
        if columns:
            tables.append(Table(name=str(table_name), columns=columns))
    return Schema(tables=tuple(tables))


def _column_from_catalogue(dialect: Any, reported: Mapping[str, Any],
                           key: str | None) -> Column:
    name = str(reported["column_name"])
    column_type = dialect.column_type_for(reported)
    return Column(
        name=name,
        type=column_type,
        nullable=str(reported["is_nullable"]).upper() == "YES",
        primary_key=(name == key),
        length=dialect.declared_length_for(reported),
        precision=(int(reported["numeric_precision"])
                   if column_type is ColumnType.DECIMAL else None),
        scale=(int(reported["numeric_scale"])
               if column_type is ColumnType.DECIMAL else None),
    )


def _primary_keys(silo: Silo, database: str, scope: str) -> dict[str, str]:
    """Each table's primary key column, where it has a single one."""
    rows = fetch_all(
        silo, database,
        "SELECT t.table_name, k.column_name "
        "FROM information_schema.table_constraints t "
        "JOIN information_schema.key_column_usage k "
        "  ON k.constraint_name = t.constraint_name "
        " AND k.table_schema = t.table_schema "
        " AND k.table_name = t.table_name "
        "WHERE t.table_schema = %s AND t.constraint_type = 'PRIMARY KEY'",
        (scope,),
    )
    return {str(table): str(column) for table, column in rows}


def verify_schema(silo: Silo, database: str, schema: Schema) -> None:
    """Raise unless the engine's catalogue agrees with the declaration.

    The only check that can actually fail. An applier that quietly
    skipped a column would satisfy anything asserted against the Schema
    object, because the Schema object is what it read.

    NAMES ARE NOT ENOUGH, and in a project about schema drift they are
    close to beside the point. A ChangeColumnType that silently did
    nothing leaves every name where it was, so the check passed and the
    simulator reported a migration that had not happened -- a lie about
    the one thing this exists to test. Types, nullability and a
    decimal's precision are compared too.

    Length is NOT compared. Engines round a declared VARCHAR up to
    their own limits and report the rounded figure, so a difference
    there says something about the engine rather than about the
    migration.
    """
    reported = read_schema(silo, database)
    for table in schema.tables:
        try:
            actual = reported.table(table.name)
        except KeyError:
            raise SiloError(
                f"{silo.name}.{database}: the engine has no table {table.name!r}, "
                f"which the schema declares"
            ) from None
        _compare_columns(silo, database, table, actual)


def _compare_columns(silo: Silo, database: str, declared: Table,
                     actual: Table) -> None:
    """What the engine says against what the pack said, column by column."""
    if [c.name for c in actual.columns] != [c.name for c in declared.columns]:
        raise SiloError(
            f"{silo.name}.{database}: table {declared.name!r} differs -- "
            f"the engine reports {[c.name for c in actual.columns]}, "
            f"the schema declares {[c.name for c in declared.columns]}"
        )
    for want, got in zip(declared.columns, actual.columns, strict=True):
        if got.type is not want.type:
            raise SiloError(
                f"{silo.name}.{database}: {declared.name}.{want.name} is "
                f"{got.type.value} in the engine, {want.type.value} in the schema"
            )
        if got.nullable != want.nullable:
            raise SiloError(
                f"{silo.name}.{database}: {declared.name}.{want.name} is "
                f"{'nullable' if got.nullable else 'NOT NULL'} in the engine, "
                f"{'nullable' if want.nullable else 'NOT NULL'} in the schema"
            )
        if want.type is ColumnType.DECIMAL and (
                got.precision != want.precision or got.scale != want.scale):
            # The one place a silent difference costs money: a column
            # declared (19,4) and created (10,0) loses the pence and
            # reports no error at all.
            raise SiloError(
                f"{silo.name}.{database}: {declared.name}.{want.name} is "
                f"DECIMAL({got.precision},{got.scale}) in the engine, "
                f"DECIMAL({want.precision},{want.scale}) in the schema"
            )


def _has_column(table: Table, name: str) -> bool:
    return any(column.name == name for column in table.columns)


def all_table_names(silo: Silo, database: str) -> list[str]:
    """Business tables the engine reports, sorted.

    information_schema on MariaDB shows every database's tables from
    any connection, so the table_schema filter is not optional there --
    without it a silo reports the catalogue of the server rather than
    of the database.
    """
    rows = fetch_all(
        silo, database,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s ORDER BY table_name",
        (_catalogue_scope(silo, database),),
    )
    return [str(row[0]) for row in rows]


def _catalogue_scope(silo: Silo, database: str) -> str:
    """What `table_schema` means on this engine.

    A genuine divergence rather than a quirk. On MariaDB a schema and a
    database are the same thing, so table_schema is the database name.
    On PostgreSQL they are different: a database contains schemas, and
    ordinary tables land in `public`. Filtering by the database name
    there returns nothing at all -- silently, as an empty list, which
    is the failure a caller would misread as "no tables".
    """
    return "public" if silo.kind == "postgresql" else database


def apply_and_verify(silo: Silo, database: str, schema: Schema) -> None:
    """Create a database, apply a schema to it, and prove it landed.

    The whole provisioning path in one call, because every caller wants
    all three and the verification is the part that would get dropped.
    """
    create_database(silo, database)
    apply_schema(silo, database, schema)
    verify_schema(silo, database, schema)


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): there is no PostgresStore/MariaDbStore pair.
# Everything here uses only PEP 249, which both drivers implement, and the
# genuinely engine-specific parts are already handled where they belong --
# connection arguments by each silo, SQL text by each dialect. A third parallel
# hierarchy would only give them a second place to be handled differently.
#
# RESOLVED: catalogue_columns filters by table_schema, not just table_name. It
# did not, and nothing noticed until several databases shared one MariaDB
# instance -- at which point verification reported a table's columns repeated
# once per database of the same name. A real pack with two databases in one
# MariaDB silo would have hit it; the tests did not, because each had a cluster
# to itself. The same omission had already been found and fixed in
# all_table_names, which is the uncomfortable part.
#
# RESOLVED: _catalogue_scope exists because table_schema means different things
# on the two engines. On MariaDB a schema is a database; on PostgreSQL a
# database contains schemas and ordinary tables land in `public`. Filtering by
# database name on PostgreSQL returns an empty list rather than an error, which
# a caller would misread as "no tables" -- a silent wrong answer, which is the
# failure class worth spending a function on.
#
# RESOLVED: insert_rows requires every row in a batch to carry identical keys.
# executemany would otherwise bind values positionally from a statement built
# off the first row, putting the wrong values in the wrong columns without
# complaint.
#
# RESOLVED: nothing here commits. Whoever owns the connection does -- connect()
# when it opened one, session() when a caller is holding one open across a
# block. An explicit commit here ended a session's transaction early, which
# made the "atomic per tick" claim in runner.py quietly false; a test that
# observed from a second connection caught it.
#
# RESOLVED: adjust_column does the arithmetic in the database rather than
# read-modify-write. One statement the engine applies atomically, against two
# round trips with a window between them -- a window that does not matter while
# the simulator is the only writer, and does the moment a consumer with
# writeback enabled touches the same row, which is a condition this tool exists
# to create.
#
# RESOLVED: an adjustment with no `where` is refused. It would silently move
# every row in the table, and is far more often a forgotten key than an
# intention.
#
# DEFERRED (known, intentional, not yet built): no general UPDATE and no DELETE. The
# aviation OOOI model needs updates -- a flight row revised four to six times
# as it passes each milestone -- and that is the pack that will bring them.
# Writing them now means guessing at how a row is addressed, which the pack
# vocabulary has not settled.
#
# RESOLVED: the reverse mapping exists now (dialect.column_type_for) and
# read_schema uses it, so a schema can be learned from a live database rather
# than only declared. It was built for a second process attaching to a running
# world; verify_schema could now compare types as well as names, and does not
# yet -- that is a separate change with its own failure modes.
#
# DEFERRED: no connection reuse. Each call opens one. Measured adequate at the
# volumes so far; a pack writing thousands of rows a tick across several tables
# will want a tick-scoped connection passed down, which is the same conclusion
# an earlier prototype reached once a pack made the cost real.
