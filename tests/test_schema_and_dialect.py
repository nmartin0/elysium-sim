"""Tests for the neutral schema and its per-engine rendering.

Two halves. The rendering tests need no server and assert what each
dialect emits. The execution tests send the generated DDL to real
PostgreSQL and MariaDB instances, because a string comparison passes
happily against SQL an engine rejects.
"""

import pytest

from simulator.dialect import MariaDbDialect, PostgresDialect, dialect_for
from simulator.ports import PortRegistry
from simulator.schema import Column, ColumnType, Table, identifier, money
from simulator.silos.mariadb import MariaDbSilo
from simulator.silos.postgres import PostgresSilo

INVOICES = Table(
    name="invoices",
    columns=(
        identifier("invoice_id"),
        Column("customer_name", ColumnType.TEXT, nullable=False, length=200),
        Column("notes", ColumnType.TEXT),
        money("total"),
        Column("line_count", ColumnType.INTEGER, nullable=False),
        Column("is_paid", ColumnType.BOOLEAN, nullable=False),
        Column("issued_on", ColumnType.DATE, nullable=False),
        Column("created_at", ColumnType.TIMESTAMP, nullable=False),
    ),
)


# -- declarations ----------------------------------------------------

def test_money_defaults_to_the_precision_accounting_uses():
    column = money("total")
    assert column.type is ColumnType.DECIMAL
    assert (column.precision, column.scale) == (19, 4)
    assert column.nullable is False


def test_a_decimal_without_precision_is_refused():
    # MySQL turns an unqualified DECIMAL into DECIMAL(10,0), which
    # stores money rounded to whole units. Enforced rather than
    # defaulted, because the default is silently destructive.
    with pytest.raises(ValueError, match="must state precision and scale"):
        Column("total", ColumnType.DECIMAL)


def test_there_is_no_float_type():
    # The absence is the point: a float cannot represent 0.10, and a
    # column of them accumulates error that surfaces as a
    # reconciliation off by pennies.
    assert "FLOAT" not in {member.name for member in ColumnType}
    assert "REAL" not in {member.name for member in ColumnType}


def test_precision_bounds_are_checked():
    with pytest.raises(ValueError, match="out of range"):
        Column("x", ColumnType.DECIMAL, precision=99, scale=2)
    with pytest.raises(ValueError, match="scale"):
        Column("x", ColumnType.DECIMAL, precision=5, scale=9)


def test_precision_on_a_non_decimal_is_refused():
    with pytest.raises(ValueError, match="only to DECIMAL"):
        Column("x", ColumnType.INTEGER, precision=5, scale=0)


def test_length_applies_only_to_text():
    with pytest.raises(ValueError, match="only to TEXT"):
        Column("x", ColumnType.INTEGER, length=10)
    with pytest.raises(ValueError, match="must be positive"):
        Column("x", ColumnType.TEXT, length=0)


def test_identifier_helper_is_a_non_null_primary_key():
    column = identifier("invoice_id")
    assert column.primary_key and not column.nullable


def test_a_nullable_primary_key_is_refused():
    with pytest.raises(ValueError, match="cannot be nullable"):
        Column("id", ColumnType.TEXT, nullable=True, primary_key=True)


def test_non_identifier_names_are_refused():
    with pytest.raises(ValueError, match="not a plain identifier"):
        Column("drop table x;--", ColumnType.TEXT)
    with pytest.raises(ValueError, match="not a plain identifier"):
        Table(name="a b", columns=(Column("x", ColumnType.TEXT),))


def test_table_rejects_duplicates_and_double_keys():
    with pytest.raises(ValueError, match="more than once"):
        Table(name="t", columns=(Column("a", ColumnType.TEXT), Column("a", ColumnType.INTEGER)))
    with pytest.raises(ValueError, match="more than one primary key"):
        Table(name="t", columns=(identifier("a"), identifier("b")))


# -- rendering -------------------------------------------------------

def test_the_two_engines_quote_identifiers_differently():
    # Not cosmetic. MariaDB reads double quotes as a string literal
    # unless ANSI_QUOTES is set, so PostgreSQL's DDL sent there is not
    # an error -- it is a table with a column named after a string.
    assert PostgresDialect().quote("order") == '"order"'
    assert MariaDbDialect().quote("order") == "`order`"


def test_reserved_words_are_quoted_so_a_pack_can_use_them():
    table = Table(name="t", columns=(Column("order", ColumnType.TEXT),))
    assert '"order"' in PostgresDialect().create_table(table)
    assert "`order`" in MariaDbDialect().create_table(table)


@pytest.mark.parametrize(("column", "postgres", "mariadb"), [
    (Column("a", ColumnType.TEXT), "TEXT", "TEXT"),
    (Column("a", ColumnType.TEXT, length=200), "TEXT", "VARCHAR(200)"),
    (Column("a", ColumnType.INTEGER), "INTEGER", "INT"),
    (Column("a", ColumnType.BIGINT), "BIGINT", "BIGINT"),
    (money("a"), "NUMERIC(19, 4)", "DECIMAL(19, 4)"),
    (Column("a", ColumnType.BOOLEAN), "BOOLEAN", "BOOLEAN"),
    (Column("a", ColumnType.DATE), "DATE", "DATE"),
    (Column("a", ColumnType.TIMESTAMP), "TIMESTAMPTZ", "DATETIME"),
])
def test_types_render_per_engine(column, postgres, mariadb):
    assert PostgresDialect().render_type(column) == postgres
    assert MariaDbDialect().render_type(column) == mariadb


def test_a_declared_length_becomes_varchar_only_where_it_matters():
    # PostgreSQL's TEXT is unbounded with no cost against VARCHAR(n);
    # honouring a length there would add a constraint the real system
    # would not have. On MariaDB the difference is real -- VARCHAR can
    # be indexed whole, TEXT needs a prefix.
    bounded = Column("name", ColumnType.TEXT, length=64)
    assert PostgresDialect().render_type(bounded) == "TEXT"
    assert MariaDbDialect().render_type(bounded) == "VARCHAR(64)"


def test_mariadb_states_engine_and_charset_explicitly():
    # Defaults vary by server version, and the combination that bites
    # is a table created as three-byte utf8 which then rejects an emoji
    # in a customer's name.
    rendered = MariaDbDialect().create_table(INVOICES)
    assert "ENGINE=InnoDB" in rendered
    assert "utf8mb4" in rendered


def test_insert_binds_values_and_quotes_names():
    statement = PostgresDialect().insert("invoices", ["invoice_id", "total"])
    assert statement == 'INSERT INTO "invoices" ("invoice_id", "total") VALUES (%s, %s)'
    with pytest.raises(ValueError, match="no columns"):
        PostgresDialect().insert("invoices", [])


def test_dialect_lookup_by_kind():
    assert dialect_for("postgresql").kind == "postgresql"
    assert dialect_for("mariadb").kind == "mariadb"
    with pytest.raises(KeyError, match="no SQL dialect"):
        # SqliteSilo exists but has no dialect yet, and the message
        # should say which kinds do rather than raise a bare KeyError.
        dialect_for("sqlite")


# -- executed against real engines -----------------------------------

@pytest.mark.postgres
def test_generated_ddl_is_valid_postgresql(tmp_path, postgres_binaries):
    # A string comparison passes happily against SQL an engine rejects,
    # so the rendering is proved by executing it.
    import psycopg

    registry = PortRegistry.allocate(tmp_path, ["ops"])
    silo = PostgresSilo(name="ops", data_dir=tmp_path / "ops",
                        port=registry.port("ops"), binaries=postgres_binaries)
    silo.create()
    silo.start()
    dialect = dialect_for(silo.kind)
    try:
        with psycopg.connect(**silo.connection_kwargs(), autocommit=True) as connection:
            connection.execute(dialect.create_database("books"))
        with psycopg.connect(**silo.connection_kwargs("books")) as connection:
            connection.execute(dialect.create_table(INVOICES))
            connection.execute(
                dialect.insert("invoices", ["invoice_id", "customer_name", "total",
                                            "line_count", "is_paid", "issued_on", "created_at"]),
                ("INV-001", "Okafor Plumbing", "1234.5600", 3, True,
                 "2026-03-02", "2026-03-02T10:15:00+00:00"),
            )
            connection.commit()
            row = connection.execute(
                'SELECT "total", "is_paid" FROM "invoices"').fetchone()
            # Exact decimal, not a float. 1234.56 has no float
            # representation, so this is the assertion that catches a
            # money column rendered as a floating type.
            assert str(row[0]) == "1234.5600"
            assert row[1] is True
    finally:
        silo.stop()


@pytest.mark.mariadb
def test_generated_ddl_is_valid_mariadb(tmp_path, mariadb_binaries):
    import pymysql

    registry = PortRegistry.allocate(tmp_path, ["shop"])
    silo = MariaDbSilo(name="shop", data_dir=tmp_path / "shop",
                       port=registry.port("shop"), binaries=mariadb_binaries)
    silo.create()
    silo.start()
    dialect = dialect_for(silo.kind)
    try:
        connection = pymysql.connect(**silo.connection_kwargs())
        try:
            with connection.cursor() as cursor:
                cursor.execute(dialect.create_database("books"))
                cursor.execute("USE `books`")
                cursor.execute(dialect.create_table(INVOICES))
                cursor.execute(
                    dialect.insert("invoices", ["invoice_id", "customer_name", "total",
                                                "line_count", "is_paid", "issued_on",
                                                "created_at"]),
                    ("INV-001", "Okafor Plumbing", "1234.5600", 3, True,
                     "2026-03-02", "2026-03-02 10:15:00"),
                )
                connection.commit()
                cursor.execute("SELECT `total`, `is_paid` FROM `invoices`")
                total, is_paid = cursor.fetchone()
                assert str(total) == "1234.5600"
                # BOOLEAN here is an alias for TINYINT(1), so a consumer
                # reads 1 rather than True. Preserved rather than
                # papered over: a consumer of the real thing meets this.
                assert is_paid == 1
        finally:
            connection.close()
    finally:
        silo.stop()


@pytest.mark.mariadb
def test_the_charset_really_survives_an_emoji(tmp_path, mariadb_binaries):
    # The failure that makes stating utf8mb4 worth it: a table created
    # as three-byte utf8 rejects this outright.
    import pymysql

    registry = PortRegistry.allocate(tmp_path, ["shop"])
    silo = MariaDbSilo(name="shop", data_dir=tmp_path / "shop",
                       port=registry.port("shop"), binaries=mariadb_binaries)
    silo.create()
    silo.start()
    dialect = dialect_for(silo.kind)
    table = Table(name="customers", columns=(identifier("customer_id"),
                                             Column("name", ColumnType.TEXT, length=100)))
    try:
        connection = pymysql.connect(**silo.connection_kwargs(), charset="utf8mb4")
        try:
            with connection.cursor() as cursor:
                cursor.execute(dialect.create_database("shop"))
                cursor.execute("USE `shop`")
                cursor.execute(dialect.create_table(table))
                cursor.execute(dialect.insert("customers", ["customer_id", "name"]),
                               ("C1", "Café Solstråle 🌞"))
                connection.commit()
                cursor.execute("SELECT `name` FROM `customers`")
                assert cursor.fetchone()[0] == "Café Solstråle 🌞"
        finally:
            connection.close()
    finally:
        silo.stop()
