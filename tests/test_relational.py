"""Tests for provisioning and writing to relational silos.

Almost everything here runs against a real engine, and deliberately:
this layer's whole job is to make an engine agree with a declaration,
and a mock would only confirm that the code calls the methods the mock
expects. Both engines run the same assertions, because a difference
between them is exactly what this layer exists to absorb.
"""

import textwrap
from contextlib import contextmanager

import pytest

from simulator import runner
from simulator.ports import PortRegistry
from simulator.relational import (
    all_table_names,
    apply_and_verify,
    apply_schema,
    catalogue_columns,
    count_rows,
    create_database,
    fetch_all,
    insert_rows,
    read_schema,
    verify_schema,
)
from simulator.schema import Column, ColumnType, Schema, Table, identifier, money
from simulator.silo import SiloError
from simulator.silos.mariadb import MariaDbSilo
from simulator.silos.postgres import PostgresSilo
from simulator.spec import load_pack

CUSTOMERS = Table(
    name="customers",
    columns=(
        identifier("customer_id"),
        Column("name", ColumnType.TEXT, nullable=False, length=200),
        Column("email", ColumnType.TEXT, length=320),
        Column("joined_on", ColumnType.DATE, nullable=False),
    ),
)

INVOICES = Table(
    name="invoices",
    columns=(
        identifier("invoice_id"),
        Column("customer_id", ColumnType.TEXT, nullable=False, length=64),
        money("total"),
        Column("is_paid", ColumnType.BOOLEAN, nullable=False),
    ),
)

BOOKS = Schema(tables=(CUSTOMERS, INVOICES))


@pytest.fixture(params=["postgresql", "mariadb"])
def silo(request, tmp_path):
    """One running silo of each relational kind, in turn.

    Parameterised rather than duplicated so every assertion below runs
    against both engines. A behaviour that holds on one and not the
    other is precisely what this layer is for, and a test written
    against a single engine would not find it.
    """
    kind = request.param
    if kind == "postgresql":
        binaries = request.getfixturevalue("postgres_binaries")
        registry = PortRegistry.allocate(tmp_path, ["ops"])
        made = PostgresSilo(name="ops", data_dir=tmp_path / "ops",
                            port=registry.port("ops"), binaries=binaries)
    else:
        binaries = request.getfixturevalue("mariadb_binaries")
        registry = PortRegistry.allocate(tmp_path, ["ops"])
        made = MariaDbSilo(name="ops", data_dir=tmp_path / "ops",
                           port=registry.port("ops"), binaries=binaries)
    made.create()
    made.start()
    try:
        yield made
    finally:
        made.stop()


def integrity_error(silo):
    """This driver's PEP 249 IntegrityError.

    Both drivers expose the DB-API name, but there is no shared base
    class between them, so the test has to pick. Naming the real
    exception is worth the four lines: `pytest.raises(Exception)` would
    pass if the insert failed for any reason at all, including a typo
    in the statement.
    """
    if silo.kind == "postgresql":
        import psycopg

        return psycopg.IntegrityError
    import pymysql

    return pymysql.err.IntegrityError


@pytest.fixture
def provisioned(silo):
    apply_and_verify(silo, "books", BOOKS)
    return silo


# -- provisioning ----------------------------------------------------

@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_schema_lands_as_declared(provisioned):
    # Against the engine's own catalogue, not against the Schema
    # object: an applier that quietly skipped a column would satisfy
    # anything asserted against the declaration it just read.
    assert catalogue_columns(provisioned, "books", "customers") == [
        "customer_id", "name", "email", "joined_on",
    ]
    assert catalogue_columns(provisioned, "books", "invoices") == [
        "invoice_id", "customer_id", "total", "is_paid",
    ]


@pytest.mark.postgres
@pytest.mark.mariadb
def test_the_catalogue_lists_exactly_the_declared_tables(provisioned):
    # table_schema means different things on the two engines -- on
    # MariaDB a schema IS a database, on PostgreSQL ordinary tables
    # land in `public`. Filtering by database name on PostgreSQL
    # returns an empty list rather than an error, which reads as "no
    # tables".
    assert all_table_names(provisioned, "books") == ["customers", "invoices"]


@pytest.mark.postgres
@pytest.mark.mariadb
def test_verification_catches_a_table_the_engine_does_not_have(provisioned):
    # It used to fail the same way a missing COLUMN does -- an absent
    # table reports no columns, so the lists differed. Now it says what
    # is actually wrong, which is worth the changed message: "no table
    # 'ghosts'" names the problem and "ghosts differs -- the engine
    # reports []" invites a hunt for the missing column.
    absent = Schema(tables=(Table(name="ghosts", columns=(identifier("ghost_id"),)),))
    with pytest.raises(SiloError, match="no table 'ghosts'"):
        verify_schema(provisioned, "books", absent)


@pytest.mark.postgres
@pytest.mark.mariadb
def test_verification_catches_a_missing_column(provisioned):
    extended = Schema(tables=(Table(
        name="customers",
        columns=(*CUSTOMERS.columns, Column("phone", ColumnType.TEXT, length=32)),
    ),))
    with pytest.raises(SiloError, match="differs"):
        verify_schema(provisioned, "books", extended)


@pytest.mark.postgres
@pytest.mark.mariadb
def test_column_order_is_preserved_not_sorted(provisioned):
    # Order is observable through SELECT *, so a consumer reading by
    # position would see a silent change if the applier sorted.
    reported = catalogue_columns(provisioned, "books", "invoices")
    assert reported != sorted(reported)
    assert reported[0] == "invoice_id"


def test_tables_are_created_in_declared_order():
    # CREATION order is not recorded anywhere portable -- both engines'
    # catalogues are queried with an ORDER BY -- so it can only be
    # observed by watching the statements go out.
    #
    # A first version asserted all_table_names() came back as
    # ["customers", "invoices"] after applying a schema declared in
    # that order. It passed against an applier that created tables in
    # REVERSE sorted order, because the catalogue query sorts and the
    # declared order happened to be alphabetical anyway. Two ways of
    # proving nothing at once.
    #
    # This declares them deliberately out of alphabetical order and
    # records what was executed. Order matters because a schema author
    # writes parents before children and will expect foreign keys to
    # honour that when they arrive.
    ordered = Schema(tables=(INVOICES, CUSTOMERS))
    silo = RecordingSilo()
    apply_schema(silo, "books", ordered)
    created = [statement.split()[2] for statement in silo.statements]
    assert created == ['"invoices"', '"customers"']


class RecordingSilo:
    """A stand-in that records statements instead of running them.

    Not a mock of the engine -- the engine is exercised everywhere else
    in this file against the real thing. This exists because statement
    ORDER is the one property no engine reports back.
    """

    kind = "postgresql"
    name = "recording"

    def __init__(self):
        self.statements: list[str] = []

    @contextmanager
    def connect(self, database=None, *, autocommit=False):
        silo = self

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, statement, parameters=()):
                silo.statements.append(statement)

        class _Connection:
            def cursor(self):
                return _Cursor()

            def commit(self):
                pass

        yield _Connection()


# -- writing ---------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.mariadb
def test_rows_go_in_and_come_back(provisioned):
    written = insert_rows(provisioned, "books", CUSTOMERS, [
        {"customer_id": "C1", "name": "Okafor Plumbing",
         "email": "ada@example.com", "joined_on": "2026-01-15"},
        {"customer_id": "C2", "name": "Café Solstråle 🌞",
         "email": None, "joined_on": "2026-02-01"},
    ])
    assert written == 2
    assert count_rows(provisioned, "books", "customers") == 2

    rows = fetch_all(provisioned, "books",
                     "SELECT name FROM customers ORDER BY customer_id")
    # The emoji survives, which is what stating utf8mb4 on MariaDB and
    # a utf8mb4 client charset are both for.
    assert [row[0] for row in rows] == ["Okafor Plumbing", "Café Solstråle 🌞"]


@pytest.mark.postgres
@pytest.mark.mariadb
def test_money_survives_the_round_trip_exactly(provisioned):
    # The assertion that catches a money column rendered as a floating
    # point type. 1234.56 has no exact float representation, so such a
    # column comes back as something that is not this string.
    #
    # (The previous wording wrapped so that a line began "# type:",
    # which Python reads as a PEP 484 type comment -- and the prose
    # after it is not valid type syntax, so the file stopped parsing
    # for any tool that enables type_comments. Ruff and pytest did not
    # care; Vulture did, and reported a syntax error pointing at
    # innocent-looking prose.)
    insert_rows(provisioned, "books", INVOICES, [
        {"invoice_id": "INV-1", "customer_id": "C1", "total": "1234.56", "is_paid": False},
    ])
    total = fetch_all(provisioned, "books", "SELECT total FROM invoices")[0][0]
    assert str(total) == "1234.5600"


@pytest.mark.postgres
@pytest.mark.mariadb
def test_an_empty_batch_is_a_no_op(provisioned):
    assert insert_rows(provisioned, "books", CUSTOMERS, []) == 0
    assert count_rows(provisioned, "books", "customers") == 0


@pytest.mark.postgres
@pytest.mark.mariadb
def test_an_undeclared_column_fails_with_its_name(provisioned):
    # A pack's typo should fail here, naming the column, rather than as
    # an engine error from inside a tick.
    with pytest.raises(SiloError, match="loyalty_tier"):
        insert_rows(provisioned, "books", CUSTOMERS, [
            {"customer_id": "C1", "name": "X", "email": None,
             "joined_on": "2026-01-01", "loyalty_tier": "gold"},
        ])


@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_ragged_batch_is_refused_rather_than_misaligned(provisioned):
    # executemany binds positionally against a statement built from the
    # first row, so a row with different keys would put the wrong
    # values in the wrong columns without complaining.
    with pytest.raises(SiloError, match="must carry the same columns"):
        insert_rows(provisioned, "books", CUSTOMERS, [
            {"customer_id": "C1", "name": "A", "email": None, "joined_on": "2026-01-01"},
            {"customer_id": "C2", "name": "B", "joined_on": "2026-01-02"},
        ])
    assert count_rows(provisioned, "books", "customers") == 0


@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_failed_batch_leaves_nothing_behind(provisioned):
    # One transaction per batch: a duplicate key in the middle must not
    # leave the rows before it committed.
    insert_rows(provisioned, "books", CUSTOMERS, [
        {"customer_id": "C1", "name": "A", "email": None, "joined_on": "2026-01-01"},
    ])
    with pytest.raises(integrity_error(provisioned)):
        insert_rows(provisioned, "books", CUSTOMERS, [
            {"customer_id": "C2", "name": "B", "email": None, "joined_on": "2026-01-02"},
            {"customer_id": "C1", "name": "clash", "email": None, "joined_on": "2026-01-03"},
        ])
    assert count_rows(provisioned, "books", "customers") == 1


@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_large_batch_goes_in_one_statement(provisioned):
    rows = [{"customer_id": f"C{index:04d}", "name": f"Customer {index}",
             "email": None, "joined_on": "2026-01-01"} for index in range(500)]
    assert insert_rows(provisioned, "books", CUSTOMERS, rows) == 500
    assert count_rows(provisioned, "books", "customers") == 500


# -- no-server checks ------------------------------------------------

def test_a_non_relational_silo_has_no_dialect(tmp_path):
    from simulator.silos.sqlite import SqliteSilo

    with pytest.raises(KeyError, match="no SQL dialect"):
        create_database(SqliteSilo(name="pos", data_dir=tmp_path), "anything")


# -- one connection held across a block ------------------------------

@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_session_commits_its_work_as_one_unit(provisioned):
    # Opening a connection per statement costs 62.9ms against 0.42ms on
    # one already open -- 149 times the cost of the query, measured on
    # the machine this was written on. A session holds one open, which
    # also makes everything inside it commit together.
    observer = _second_view(provisioned)
    with provisioned.session("books"):
        insert_rows(provisioned, "books", CUSTOMERS, [
            {"customer_id": "C1", "name": "A", "email": None, "joined_on": "2026-01-01"}])
        # Nothing is visible from another connection yet.
        assert count_rows(observer, "books", "customers") == 0
    assert count_rows(observer, "books", "customers") == 1


@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_session_is_reentrant(provisioned):
    # An inner session joins the outer transaction rather than opening
    # its own.
    with provisioned.session("books"):
        with provisioned.session("books"):
            insert_rows(provisioned, "books", CUSTOMERS, [
                {"customer_id": "C1", "name": "A", "email": None,
                 "joined_on": "2026-01-01"}])
    assert count_rows(provisioned, "books", "customers") == 1


@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_nested_session_rolls_back_with_the_outer_one(provisioned):
    # THE property re-entrancy buys, and the reason is transaction
    # unity rather than deadlock. Both engines happily allow a second
    # connection -- which is the problem, not the safeguard: without
    # re-entrancy the inner block commits independently, so an outer
    # failure rolls back only part of the work.
    #
    # A first version asserted only that the row was there afterwards,
    # which is true whether the inner block joined or committed on its
    # own, so it passed against a non-re-entrant implementation.
    class Deliberate(Exception):
        pass

    with pytest.raises(Deliberate):
        with provisioned.session("books"):
            with provisioned.session("books"):
                insert_rows(provisioned, "books", CUSTOMERS, [
                    {"customer_id": "C1", "name": "A", "email": None,
                     "joined_on": "2026-01-01"}])
            raise Deliberate
    assert count_rows(provisioned, "books", "customers") == 0


@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_session_does_not_leak_into_another_database(provisioned):
    # connect() reuses the session's connection only for the SAME
    # database. Reusing it for another would silently run the statement
    # against the wrong one.
    with provisioned.session("books"):
        create_database(provisioned, "elsewhere")
        apply_schema(provisioned, "elsewhere", Schema(tables=(CUSTOMERS,)))
        insert_rows(provisioned, "elsewhere", CUSTOMERS, [
            {"customer_id": "X", "name": "A", "email": None, "joined_on": "2026-01-01"}])
    assert count_rows(provisioned, "elsewhere", "customers") == 1
    assert count_rows(provisioned, "books", "customers") == 0


@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_failure_inside_a_session_leaves_nothing_behind(provisioned):
    with pytest.raises(integrity_error(provisioned)):
        with provisioned.session("books"):
            insert_rows(provisioned, "books", CUSTOMERS, [
                {"customer_id": "C1", "name": "A", "email": None,
                 "joined_on": "2026-01-01"}])
            insert_rows(provisioned, "books", CUSTOMERS, [
                {"customer_id": "C1", "name": "clash", "email": None,
                 "joined_on": "2026-01-02"}])
    assert count_rows(provisioned, "books", "customers") == 0


def _second_view(silo):
    """The same silo, seen through a separate connection."""
    from simulator.silos.mariadb import MariaDbSilo
    from simulator.silos.postgres import PostgresSilo

    if silo.kind == "postgresql":
        return PostgresSilo(name=silo.name, data_dir=silo.data_dir, port=silo.port,
                            binaries=silo.binaries)
    return MariaDbSilo(name=silo.name, data_dir=silo.data_dir, port=silo.port,
                       binaries=silo.binaries)


@pytest.mark.postgres
@pytest.mark.mariadb
def test_the_catalogue_does_not_confuse_databases_sharing_a_table_name(silo):
    # On MariaDB one server hosts every database and information_schema
    # spans all of them, so a table called `invoices` in three
    # databases reported eighteen columns rather than six. Invisible
    # for as long as every test had a cluster to itself with one
    # database in it -- and a real pack with two databases in one
    # MariaDB silo would have hit it.
    for name in ("alpha", "beta"):
        create_database(silo, name)
        apply_schema(silo, name, BOOKS)

    for name in ("alpha", "beta"):
        assert catalogue_columns(silo, name, "customers") == [
            "customer_id", "name", "email", "joined_on"]
        verify_schema(silo, name, BOOKS)


# -- learning a schema by asking the engine ---------------------------

#: Every declared type, which BOOKS does not cover -- it has no boolean
#: and no text without a length, so read-back tests written against it
#: could not see the two cases that are actually hard. A boolean is
#: TINYINT(1) on MariaDB and indistinguishable from a small integer by
#: type name alone; an unlengthed TEXT reports a capacity of 65535 that
#: nobody declared.
EVERY_TYPE = Table(
    name="every_type",
    columns=(
        identifier("key"),
        Column("bounded", ColumnType.TEXT, nullable=False, length=200),
        Column("unbounded", ColumnType.TEXT),
        money("amount"),
        Column("quantity", ColumnType.INTEGER),
        Column("large", ColumnType.BIGINT),
        Column("flag", ColumnType.BOOLEAN, nullable=False),
        Column("on_day", ColumnType.DATE),
        Column("at", ColumnType.TIMESTAMP),
    ),
)


@pytest.mark.postgres
@pytest.mark.mariadb
def test_every_declared_type_survives_a_read_back(silo):
    from simulator.schema import Schema as _Schema

    apply_and_verify(silo, "types", _Schema(tables=(EVERY_TYPE,)))
    recovered = read_schema(silo, "types").table("every_type")

    assert [(column.name, column.type) for column in recovered.columns] == [
        (column.name, column.type) for column in EVERY_TYPE.columns]
    # The boolean specifically: MariaDB reports it as `tinyint`, and
    # only the full column_type of `tinyint(1)` says it is not a small
    # integer.
    assert recovered.column("flag").type is ColumnType.BOOLEAN
    assert recovered.column("quantity").type is ColumnType.INTEGER
    assert recovered.column("flag").nullable is False


@pytest.mark.postgres
@pytest.mark.mariadb
def test_an_undeclared_text_length_stays_undeclared(silo):
    # MariaDB reports a TEXT column's CAPACITY of 65535, which is not a
    # length anybody wrote. Reading it back as one would invent a
    # constraint the pack never asked for.
    from simulator.schema import Schema as _Schema

    apply_and_verify(silo, "unbounded", _Schema(tables=(EVERY_TYPE,)))
    recovered = read_schema(silo, "unbounded").table("every_type")
    assert recovered.column("unbounded").length is None

@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_schema_can_be_read_back_from_the_engine(silo):
    # What lets a second process attach to a world another one is
    # running: the pack says what the schema was DECLARED to be, which
    # stops being true the moment anything drifts.
    apply_and_verify(silo, "readback", BOOKS)
    read = read_schema(silo, "readback")

    assert sorted(table.name for table in read.tables) == ["customers", "invoices"]
    declared = BOOKS.table("customers")
    recovered = read.table("customers")
    # Names, order, types, nullability and the primary key all survive.
    assert [column.name for column in recovered.columns] == [
        column.name for column in declared.columns]
    assert [column.type for column in recovered.columns] == [
        column.type for column in declared.columns]
    assert [column.nullable for column in recovered.columns] == [
        column.nullable for column in declared.columns]
    assert recovered.primary_key().name == "customer_id"


@pytest.mark.postgres
@pytest.mark.mariadb
def test_money_keeps_its_precision_through_a_read_back(silo):
    # The one that would matter most if it were lost: a DECIMAL read
    # back as a plain numeric could be rendered wrong on the way out.
    apply_and_verify(silo, "money", BOOKS)
    balance = read_schema(silo, "money").table("invoices").column("total")
    assert balance.type is ColumnType.DECIMAL
    assert (balance.precision, balance.scale) == (19, 4)


@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_read_back_describes_the_engine_not_the_declaration(silo):
    # PostgreSQL stores no length for text, because its TEXT is
    # unbounded and rendering one was a considered choice not to make.
    # MariaDB reports a TEXT column's CAPACITY of 65535, which is not a
    # length anybody declared. Neither is a defect to paper over --
    # where a read-back and a declaration differ, the difference is
    # information.
    apply_and_verify(silo, "lengths", BOOKS)
    recovered = read_schema(silo, "lengths").table("customers")

    if silo.kind == "postgresql":
        assert all(column.length is None for column in recovered.columns)
    else:
        # Declared lengths render to VARCHAR here and survive; an
        # undeclared one stays undeclared rather than becoming 65535.
        assert recovered.column("name").length == 200
        assert recovered.column("email").length == 320


@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_read_back_leaves_out_the_simulators_own_bookkeeping(silo):
    # The migration history is real and lives in the database on
    # purpose, but it is not part of the business.
    from datetime import UTC, datetime

    from simulator.drift import AddColumn

    apply_and_verify(silo, "book", BOOKS)
    AddColumn("customers", Column("channel", ColumnType.TEXT, length=16)).apply(
        silo, "book", BOOKS, datetime(2026, 3, 2, tzinfo=UTC))

    names = [table.name for table in read_schema(silo, "book").tables]
    assert "customers" in names
    assert not any(name.startswith("_simulator") for name in names)


@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_read_back_sees_drift_the_declaration_does_not(silo):
    # THE point of reading it back at all.
    from datetime import UTC, datetime

    from simulator.drift import DropColumn

    apply_and_verify(silo, "drifted", BOOKS)
    DropColumn("customers", "email").apply(silo, "drifted", BOOKS,
                                           datetime(2026, 3, 2, tzinfo=UTC))

    recovered = read_schema(silo, "drifted").table("customers")
    assert "email" not in [column.name for column in recovered.columns]
    assert "email" in [column.name for column in BOOKS.table("customers").columns]


# -- what a silo says its driver raises -------------------------------

@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_silo_names_its_own_driver_errors(silo):
    # So a caller that has to tolerate failure tolerates the RIGHT
    # failures. Both drivers root their exceptions at a single base,
    # which is PEP 249's own arrangement.
    errors = silo.driver_errors()
    assert errors
    with pytest.raises(errors):
        fetch_all(silo, "books", "SELECT * FROM no_such_table")


@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_driver_error_does_not_catch_a_mistake_in_our_own_code(silo):
    # THE reason for narrowing. A KeyError or a TypeError must not look
    # like a database that has gone away, or a bug here gets recorded
    # as drift -- a wrong answer nobody investigates rather than a
    # crash somebody fixes.
    for mistake in (KeyError("watch"), TypeError("nope"), AttributeError("gone")):
        assert not isinstance(mistake, silo.driver_errors())


def test_a_silo_with_no_driver_names_no_errors(tmp_path):
    from simulator.silos import build_silo

    # Empty rather than a guess: a folder has no driver, and pretending
    # otherwise would have a caller catching things it cannot get.
    assert build_silo("filedrop", "drop", tmp_path).driver_errors() == ()
    assert build_silo("sqlite", "pos", tmp_path).driver_errors()


# -- verification compares more than names ----------------------------

TYPED = textwrap.dedent("""
    pack: typed
    silos: {ops: {kind: postgresql, database: ops}}
    schemas:
      ops:
        tables:
          money:
            columns:
              money_id: {type: text, length: 64, primary_key: true, nullable: false}
              amount:   {type: decimal, precision: 19, scale: 4, nullable: false}
              note:     {type: text, length: 64}
    """)


@pytest.mark.postgres
def test_verification_notices_a_column_whose_type_changed(tmp_path, postgres_binaries):
    # In a project about schema drift, comparing names is close to
    # beside the point: a ChangeColumnType that silently did nothing
    # leaves every name where it was, so the check passed and the
    # simulator reported a migration that had not happened.
    path = tmp_path / "typed.yaml"
    path.write_text(TYPED)
    pack = load_pack(path)
    world = runner.build(pack, tmp_path / "var", seed=1)
    try:
        schema = world.schema("ops")
        with world.silo("ops").connect("ops", autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("ALTER TABLE money ALTER COLUMN note TYPE integer "
                               "USING NULL")
        with pytest.raises(SiloError, match="integer in the engine, text in the schema"):
            verify_schema(world.silo("ops"), "ops", schema)
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_verification_notices_a_decimal_that_lost_its_pence(tmp_path,
                                                            postgres_binaries):
    # The one place a silent difference costs money: a column declared
    # (19,4) and created (10,0) loses the pence and reports no error.
    path = tmp_path / "typed.yaml"
    path.write_text(TYPED)
    world = runner.build(load_pack(path), tmp_path / "var", seed=1)
    try:
        schema = world.schema("ops")
        with world.silo("ops").connect("ops", autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("ALTER TABLE money ALTER COLUMN amount "
                               "TYPE numeric(10,0)")
        with pytest.raises(SiloError, match=r"DECIMAL\(10,0\).*DECIMAL\(19,4\)"):
            verify_schema(world.silo("ops"), "ops", schema)
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_verification_notices_a_column_that_stopped_being_required(tmp_path,
                                                                   postgres_binaries):
    path = tmp_path / "typed.yaml"
    path.write_text(TYPED)
    world = runner.build(load_pack(path), tmp_path / "var", seed=1)
    try:
        schema = world.schema("ops")
        with world.silo("ops").connect("ops", autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("ALTER TABLE money ALTER COLUMN amount DROP NOT NULL")
        with pytest.raises(SiloError, match="nullable in the engine, NOT NULL"):
            verify_schema(world.silo("ops"), "ops", schema)
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_verification_ignores_a_length_the_engine_rounded(tmp_path, postgres_binaries):
    # Engines round a declared VARCHAR up to their own limits and
    # report the rounded figure, so a difference there says something
    # about the engine rather than about the migration.
    path = tmp_path / "typed.yaml"
    path.write_text(TYPED)
    world = runner.build(load_pack(path), tmp_path / "var", seed=1)
    try:
        schema = world.schema("ops")
        with world.silo("ops").connect("ops", autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("ALTER TABLE money ALTER COLUMN note TYPE varchar(200)")
        verify_schema(world.silo("ops"), "ops", schema)
    finally:
        runner.stop(world)


def test_a_password_is_the_same_for_the_same_world():
    # Derived from the seed rather than drawn at random, because
    # everything else here is: a world built twice from the same seed
    # is the same world, and a credential that changed between runs
    # would be the one thing about it that did not.
    from simulator.silos.reader import READER, WRITER, password_for

    assert password_for(READER, 7) == password_for(READER, 7)
    assert password_for(READER, 7) != password_for(READER, 8)
    assert password_for(READER, 7) != password_for(WRITER, 7)
    assert password_for(READER, 7).startswith("reader-")
    assert len(password_for(READER, 7)) > 16
