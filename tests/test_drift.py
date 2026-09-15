"""Tests for changing a database's shape while it is being used.

The reason this project exists. Every case runs against BOTH engines
through one parameterised fixture, because the operations that matter
most -- retyping especially -- are spelled completely differently by
the two, and a test against one would not find it.

The assertions are against the ENGINE's catalogue, never against the
schema object: an operation that revised the object and left the
database alone would satisfy anything asked of the object, since the
object is what it edited.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from simulator.drift import (
    AddColumn,
    AddTable,
    ChangeColumnType,
    DriftError,
    DropColumn,
    DropTable,
    RenameColumn,
    RenameTable,
    RescaleColumn,
    history,
)
from simulator.ports import PortRegistry
from simulator.relational import apply_and_verify, catalogue_columns, fetch_all, insert_rows
from simulator.schema import Column, ColumnType, Schema, Table, identifier, money
from simulator.silos.mariadb import MariaDbSilo
from simulator.silos.postgres import PostgresSilo

INVOICES = Table(
    name="invoices",
    columns=(
        identifier("invoice_id"),
        Column("customer", ColumnType.TEXT, nullable=False, length=200),
        money("total"),
        Column("reference", ColumnType.TEXT, length=64),
        # Numeric TEXT, so a retype to DECIMAL is a cast PostgreSQL
        # will NOT do implicitly -- which is what exercises USING.
        Column("amount_text", ColumnType.TEXT, length=32),
        # Nullable money, so a rescale has a null to leave alone.
        Column("discount", ColumnType.DECIMAL, precision=19, scale=4),
    ),
)
SCHEMA = Schema(tables=(INVOICES,))
AT = datetime(2026, 3, 2, 10, 15, tzinfo=UTC)


@pytest.fixture(params=["postgresql", "mariadb"])
def silo(request, tmp_path):
    kind = request.param
    registry = PortRegistry.allocate(tmp_path, ["ops"])
    if kind == "postgresql":
        made = PostgresSilo(name="ops", data_dir=tmp_path / "ops",
                            port=registry.port("ops"),
                            binaries=request.getfixturevalue("postgres_binaries"))
    else:
        made = MariaDbSilo(name="ops", data_dir=tmp_path / "ops",
                           port=registry.port("ops"),
                           binaries=request.getfixturevalue("mariadb_binaries"))
    made.create()
    made.start()
    try:
        apply_and_verify(made, "books", SCHEMA)
        insert_rows(made, "books", INVOICES, [
            {"invoice_id": "INV-1", "customer": "Okafor", "total": "100.0000",
             "reference": "PO-9", "amount_text": "12.50", "discount": "5.0000"},
            {"invoice_id": "INV-2", "customer": "Feldman", "total": "250.5000",
             "reference": None, "amount_text": "7.25", "discount": None},
        ])
        yield made
    finally:
        made.stop()


def columns(silo, table="invoices"):
    return catalogue_columns(silo, "books", table)


def rows(silo, statement):
    return fetch_all(silo, "books", statement)


# -- additive: nothing downstream should notice ----------------------

@pytest.mark.postgres
@pytest.mark.mariadb
def test_adding_a_column_leaves_existing_rows_intact(silo):
    AddColumn("invoices", Column("channel", ColumnType.TEXT, length=32)).apply(
        silo, "books", SCHEMA, AT)
    assert columns(silo) == ["invoice_id", "customer", "total", "reference",
                             "amount_text", "discount", "channel"]
    assert [row[0] for row in rows(silo, "SELECT channel FROM invoices")] == [None, None]
    assert rows(silo, "SELECT count(*) FROM invoices")[0][0] == 2


@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_not_null_column_cannot_be_added_to_a_populated_table(silo):
    # Both engines refuse it, and rightly -- existing rows would have
    # no value. Raising here names the real reason instead of passing
    # through the engine's terser complaint.
    with pytest.raises(DriftError, match="NOT NULL"):
        AddColumn("invoices", Column("region", ColumnType.TEXT, nullable=False,
                                     length=32)).apply(silo, "books", SCHEMA, AT)


@pytest.mark.postgres
@pytest.mark.mariadb
def test_adding_a_table_is_invisible_to_the_old_one(silo):
    credits = Table(name="credits", columns=(identifier("credit_id"), money("amount")))
    revised = AddTable(credits).apply(silo, "books", SCHEMA, AT)
    assert revised.has_table("credits")
    assert columns(silo)[:4] == ["invoice_id", "customer", "total", "reference"]


# -- destructive: it must fail WELL ----------------------------------

@pytest.mark.postgres
@pytest.mark.mariadb
def test_dropping_a_column_removes_it_from_the_engine(silo):
    DropColumn("invoices", "reference").apply(silo, "books", SCHEMA, AT)
    assert "reference" not in columns(silo)


@pytest.mark.postgres
@pytest.mark.mariadb
def test_the_primary_key_cannot_be_dropped(silo):
    with pytest.raises(DriftError, match="primary key"):
        DropColumn("invoices", "invoice_id").apply(silo, "books", SCHEMA, AT)


@pytest.mark.postgres
@pytest.mark.mariadb
def test_renaming_leaves_the_data_one_name_away(silo):
    # Nastier than a drop from a consumer's view: every value is still
    # there, so the failure reads as a missing column while the data
    # sits untouched under the new name.
    RenameColumn("invoices", "reference", "po_number").apply(silo, "books", SCHEMA, AT)
    assert columns(silo)[3] == "po_number"
    assert rows(silo, "SELECT po_number FROM invoices ORDER BY invoice_id")[0][0] == "PO-9"


@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_rename_does_not_reorder_the_columns(silo):
    # Column order is observable through SELECT *, so a rename that
    # reordered would itself be an unintended schema change.
    RenameColumn("invoices", "customer", "customer_name").apply(silo, "books", SCHEMA, AT)
    assert columns(silo)[1] == "customer_name"


@pytest.mark.postgres
@pytest.mark.mariadb
def test_dropping_and_renaming_a_table(silo):
    revised = RenameTable("invoices", "sales_invoices").apply(silo, "books", SCHEMA, AT)
    assert columns(silo, "sales_invoices")[:3] == ["invoice_id", "customer", "total"]
    DropTable("sales_invoices").apply(silo, "books", revised, AT)
    assert rows(silo, "SELECT count(*) FROM information_schema.tables "
                      "WHERE table_name = 'sales_invoices'")[0][0] == 0


# -- retyping: where the engines disagree most -----------------------

@pytest.mark.postgres
@pytest.mark.mariadb
def test_retyping_keeps_what_is_already_in_the_column(silo):
    ChangeColumnType("invoices", "reference",
                     Column("reference", ColumnType.TEXT, length=200)).apply(
        silo, "books", SCHEMA, AT)
    assert rows(silo, "SELECT reference FROM invoices ORDER BY invoice_id")[0][0] == "PO-9"


@pytest.mark.postgres
@pytest.mark.mariadb
def test_retyping_across_types_the_engine_will_not_cast_implicitly(silo):
    # THE case that exercises PostgreSQL's USING clause. A first
    # version retyped NUMERIC(19,4) to NUMERIC(26,8), which PostgreSQL
    # casts implicitly -- so it passed with USING removed entirely.
    # Text to decimal is a conversion it refuses without one, failing
    # with "column cannot be cast automatically".
    ChangeColumnType("invoices", "amount_text",
                     Column("amount_text", ColumnType.DECIMAL,
                            precision=19, scale=4)).apply(silo, "books", SCHEMA, AT)
    amounts = [row[0] for row in
               rows(silo, "SELECT amount_text FROM invoices ORDER BY invoice_id")]
    assert amounts == [Decimal("12.5000"), Decimal("7.2500")]


@pytest.mark.postgres
@pytest.mark.mariadb
def test_retyping_money_to_a_wider_decimal_keeps_it_exact(silo):
    ChangeColumnType("invoices", "total", money("total", precision=26, scale=8)).apply(
        silo, "books", SCHEMA, AT)
    total = rows(silo, "SELECT total FROM invoices ORDER BY invoice_id")[0][0]
    assert total == Decimal("100")
    assert total.as_tuple().exponent == -8


@pytest.mark.postgres
@pytest.mark.mariadb
def test_retyping_keeps_a_column_not_null(silo):
    # MariaDB's MODIFY COLUMN restates the whole definition, so a
    # dropped NOT NULL would be a change nobody asked for arriving
    # inside a change they did.
    ChangeColumnType("invoices", "customer",
                     Column("customer", ColumnType.TEXT, nullable=False,
                            length=400)).apply(silo, "books", SCHEMA, AT)
    nullable = rows(silo, "SELECT is_nullable FROM information_schema.columns "
                          "WHERE table_name = 'invoices' AND column_name = 'customer'")
    assert nullable[0][0] == "NO"


@pytest.mark.postgres
@pytest.mark.mariadb
def test_retyping_cannot_secretly_rename(silo):
    with pytest.raises(DriftError, match="cannot also rename"):
        ChangeColumnType("invoices", "reference",
                         Column("po_number", ColumnType.TEXT, length=64)).apply(
            silo, "books", SCHEMA, AT)


# -- the sharp one ----------------------------------------------------

@pytest.mark.postgres
@pytest.mark.mariadb
def test_rescaling_changes_the_answers_without_changing_anything_else(silo):
    # THE failure class that matters most. No structural change, every
    # read succeeds, every type still checks -- and the numbers are now
    # wrong unless the consumer noticed. Dollars becoming cents.
    before = columns(silo)
    RescaleColumn("invoices", "total", Decimal(100)).apply(silo, "books", SCHEMA, AT)
    assert columns(silo) == before
    totals = [row[0] for row in rows(silo, "SELECT total FROM invoices ORDER BY invoice_id")]
    assert totals == [Decimal("10000.0000"), Decimal("25050.0000")]


@pytest.mark.postgres
@pytest.mark.mariadb
def test_rescaling_leaves_nulls_alone(silo):
    # Against the RESCALED column. A first version checked nulls in a
    # different column entirely, which no rescale could have touched,
    # so it passed against an implementation coercing them to zero.
    RescaleColumn("invoices", "discount", Decimal(100)).apply(silo, "books", SCHEMA, AT)
    discounts = [row[0] for row in
                 rows(silo, "SELECT discount FROM invoices ORDER BY invoice_id")]
    assert discounts == [Decimal("500.0000"), None]


# -- attribution ------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.mariadb
def test_every_change_is_recorded_with_its_severity(silo):
    revised = AddColumn("invoices", Column("channel", ColumnType.TEXT, length=32)).apply(
        silo, "books", SCHEMA, AT)
    DropColumn("invoices", "reference").apply(silo, "books", revised, AT)

    entries = history(silo, "books")
    assert [entry["operation"] for entry in entries] == ["AddColumn", "DropColumn"]
    assert [entry["breaking"] for entry in entries] == [False, True]
    assert entries[0]["detail"] == "added invoices.channel (text)"


@pytest.mark.postgres
@pytest.mark.mariadb
def test_the_history_table_is_not_mistaken_for_a_business_table(silo):
    # It genuinely exists -- real databases carry one, which is why it
    # lives inside the simulated database at all -- but verification
    # must not treat it as part of the declared schema.
    AddColumn("invoices", Column("channel", ColumnType.TEXT, length=32)).apply(
        silo, "books", SCHEMA, AT)
    assert "_simulator_migrations" not in columns(silo)


# -- verification -----------------------------------------------------

@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_change_that_did_not_take_effect_is_caught(silo):
    # The guarantee that makes all of the above trustworthy. A
    # subclass that revises the schema object and touches nothing must
    # be caught, and only a catalogue comparison can catch it.
    class Liar(AddColumn):
        def statements(self, silo, schema):
            return []

    with pytest.raises(DriftError, match="did not change as declared"):
        Liar("invoices", Column("ghost", ColumnType.TEXT, length=32)).apply(
            silo, "books", SCHEMA, AT)


def test_the_breaking_flags_follow_foundrys_taxonomy():
    # Additive changes pass through untouched in Foundry; destructive
    # ones are refused by default and need manual remapping. These are
    # their list, not a judgement made locally.
    assert AddColumn.is_breaking is False
    assert AddTable.is_breaking is False
    for operation in (DropColumn, RenameColumn, ChangeColumnType, DropTable,
                      RenameTable, RescaleColumn):
        assert operation.is_breaking is True, operation.__name__
