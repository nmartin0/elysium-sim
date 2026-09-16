"""Tests for packs that declare their own schema drift.

What makes drift part of a simulation rather than something invoked by
hand: a pack says the business added a loyalty column on day forty,
and on day forty it appears underneath whatever is reading.
"""

import textwrap

import pytest
from worlds import running_world, write_pack

from simulator import runner
from simulator.drift import history
from simulator.relational import catalogue_columns, fetch_all
from simulator.spec import PackError, load_spec

SHOP = textwrap.dedent("""
    pack: drifting_shop

    silos:
      ops: {kind: postgresql, database: ops}

    schemas:
      ops:
        tables:
          orders:
            columns:
              order_id:  {type: text, length: 64, primary_key: true, nullable: false}
              customer:  {type: text, length: 120, nullable: false}
              total:     {type: decimal, precision: 19, scale: 4, nullable: false}
              placed_at: {type: timestamp, nullable: false}
          seeds:
            columns:
              seed_id: {type: text, length: 64, primary_key: true, nullable: false}

    seed:
      - table: ops.seeds
        count: 3
        columns:
          seed_id: {generator: id, prefix: s}

    events:
      order_placed:
        per: ops.seeds
        rate_per_hour: 0.5
        emits:
          - table: ops.orders
            columns:
              order_id:  {generator: id, prefix: ord}
              customer:  {generator: choice, options: [Okafor, Feldman]}
              total:     {generator: decimal, min: 10, max: 400, scale: 4}
              placed_at: {generator: now}

    migrations:
      - at: 3d
        operation: add_column
        table: ops.orders
        column: loyalty_tier
        type: text
        length: 16
      - at: 6d
        operation: rescale_column
        table: ops.orders
        column: total
        factor: 100
    """)


@pytest.fixture
def world(tmp_path, postgres_binaries):
    with running_world(tmp_path, SHOP, seed=8) as built:
        yield built


def columns(world):
    return catalogue_columns(world.silo("ops"), "ops", "orders")


def average_total(world):
    return fetch_all(world.silo("ops"), "ops", "SELECT round(avg(total), 2) FROM orders")[0][0]


# -- drift happens on schedule ---------------------------------------

@pytest.mark.postgres
def test_a_column_appears_on_the_day_the_pack_says(world):
    runner.run(world, total_seconds=2 * 86400, tick_seconds=3600)
    assert "loyalty_tier" not in columns(world)
    runner.run(world, total_seconds=2 * 86400, tick_seconds=3600)
    assert "loyalty_tier" in columns(world)


@pytest.mark.postgres
def test_an_additive_migration_changes_nothing_else(world):
    runner.run(world, total_seconds=2 * 86400, tick_seconds=3600)
    before = average_total(world)
    rows_before = fetch_all(world.silo("ops"), "ops", "SELECT count(*) FROM orders")[0][0]

    runner.run(world, total_seconds=2 * 86400, tick_seconds=3600)

    assert columns(world)[:4] == ["order_id", "customer", "total", "placed_at"]
    # New orders arrived, and the old ones are untouched apart from a
    # null in the new column.
    assert fetch_all(world.silo("ops"), "ops", "SELECT count(*) FROM orders")[0][0] > rows_before
    assert fetch_all(world.silo("ops"), "ops",
                     "SELECT count(*) FROM orders WHERE loyalty_tier IS NOT NULL")[0][0] == 0
    assert abs(average_total(world) - before) < before / 2


@pytest.mark.postgres
def test_a_rescale_changes_the_answers_and_nothing_else(world):
    runner.run(world, total_seconds=5 * 86400, tick_seconds=3600)
    before, shape = average_total(world), columns(world)

    runner.run(world, total_seconds=86400, tick_seconds=3600)

    assert columns(world) == shape
    assert average_total(world) > before * 10


@pytest.mark.postgres
def test_after_a_rescale_the_column_holds_two_units_at_once(world):
    # THE authentic consequence of a botched migration, and the one
    # nothing raises on. The simulation keeps writing at the old scale,
    # so the column becomes a mix of dollars and cents -- and the
    # average drifts back down as new rows dilute the rescaled ones.
    runner.run(world, total_seconds=6 * 86400, tick_seconds=3600)
    just_after = average_total(world)
    runner.run(world, total_seconds=2 * 86400, tick_seconds=3600)
    later = average_total(world)

    assert later < just_after
    # Both units present: rescaled rows far above anything the
    # generator produces, and fresh ones inside its range.
    big = fetch_all(world.silo("ops"), "ops",
                    "SELECT count(*) FROM orders WHERE total > 1000")[0][0]
    small = fetch_all(world.silo("ops"), "ops",
                      "SELECT count(*) FROM orders WHERE total <= 400")[0][0]
    assert big > 0 and small > 0


@pytest.mark.postgres
def test_a_tick_longer_than_the_gap_applies_both(world):
    # Due-ness is computed from the clock rather than remembered, so a
    # single long tick must not skip one.
    runner.run(world, total_seconds=8 * 86400, tick_seconds=8 * 86400)
    assert "loyalty_tier" in columns(world)
    assert [entry["operation"] for entry in history(world.silo("ops"), "ops")] == [
        "AddColumn", "RescaleColumn"]


@pytest.mark.postgres
def test_each_migration_runs_once(world):
    runner.run(world, total_seconds=10 * 86400, tick_seconds=3600)
    entries = history(world.silo("ops"), "ops")
    assert len(entries) == 2
    # Run twice, a rescale would multiply by ten thousand.
    assert average_total(world) < 100_000


@pytest.mark.postgres
def test_the_history_records_what_happened_and_how_badly(world):
    runner.run(world, total_seconds=8 * 86400, tick_seconds=3600)
    entries = history(world.silo("ops"), "ops")
    assert [entry["breaking"] for entry in entries] == [False, True]
    assert entries[0]["detail"] == "added orders.loyalty_tier (text)"
    assert "rescaled orders.total by 100" in entries[1]["detail"]


@pytest.mark.postgres
def test_the_running_schema_diverges_from_the_declared_one(world):
    # A running world's schema is not the pack's once anything has
    # drifted, and everything writing rows must read the running one or
    # a pack would keep writing to a column its own migration dropped.
    runner.run(world, total_seconds=4 * 86400, tick_seconds=3600)
    running = {column.name for column in world.schema("ops").table("orders").columns}
    declared = {column.name for column in world.pack.schemas["ops"].table("orders").columns}
    assert "loyalty_tier" in running
    assert "loyalty_tier" not in declared


@pytest.mark.postgres
def test_dropping_a_column_its_own_events_write_fails_loudly(tmp_path,
                                                             postgres_binaries):
    # A migration removing something still in use is how real outages
    # happen, and the pack breaking itself is the right outcome. What
    # matters is that it fails LOUDLY, naming the column, rather than
    # writing nulls or carrying on.
    #
    # A dedicated pack rather than editing SHOP by string replacement:
    # SHOP has been through textwrap.dedent, so the indentation in the
    # file is not the indentation in the string, and a replace written
    # against the file silently matches nothing. That produced a test
    # which ran the UNMODIFIED pack and then wondered why it did not
    # fail.
    source = textwrap.dedent("""
        pack: dropping

        silos:
          ops: {kind: postgresql, database: ops}

        schemas:
          ops:
            tables:
              orders:
                columns:
                  order_id: {type: text, length: 64, primary_key: true, nullable: false}
                  customer: {type: text, length: 120, nullable: false}
              seeds:
                columns:
                  seed_id: {type: text, length: 64, primary_key: true, nullable: false}

        seed:
          - table: ops.seeds
            count: 2
            columns:
              seed_id: {generator: id, prefix: s}

        events:
          order_placed:
            per: ops.seeds
            rate_per_hour: 2.0
            emits:
              - table: ops.orders
                columns:
                  order_id: {generator: id, prefix: ord}
                  customer: {generator: constant, value: Okafor}

        migrations:
          - at: 2d
            operation: drop_column
            table: ops.orders
            column: customer
        """)
    built = runner.build(write_pack(tmp_path, source, "dropping"), tmp_path / "d", seed=8)
    try:
        runner.seed(built)
        runner.run(built, total_seconds=86400, tick_seconds=3600)
        with pytest.raises(Exception, match=r"customer"):
            runner.run(built, total_seconds=2 * 86400, tick_seconds=3600)
    finally:
        runner.stop(built)


# -- the timeline, checked at load ------------------------------------

def base(migrations) -> dict:
    return {
        "pack": "x",
        "silos": {"ops": {"kind": "postgresql", "database": "ops"}},
        "schemas": {"ops": {"tables": {"orders": {"columns": {
            "order_id": {"type": "text", "length": 64, "primary_key": True,
                         "nullable": False},
            "total": {"type": "decimal", "precision": 19, "scale": 4}}}}}},
        "migrations": migrations,
    }


def test_the_timeline_is_validated_against_itself():
    # Each migration is applied to a copy of the schema the previous
    # one left, so this fails when the file is read rather than on day
    # ninety of a run.
    with pytest.raises(PackError, match="no column 'total'"):
        load_spec(base([
            {"at": "1d", "operation": "drop_column", "table": "ops.orders",
             "column": "total"},
            {"at": "2d", "operation": "drop_column", "table": "ops.orders",
             "column": "total"},
        ]))


def test_referring_to_a_column_a_previous_migration_renamed_is_refused():
    # A rescale changes no structure, so its revise() is a no-op and
    # validated nothing until the column check was moved into the
    # builder. This test is what found that.
    with pytest.raises(PackError, match="no column 'total'"):
        load_spec(base([
            {"at": "1d", "operation": "rename_column", "table": "ops.orders",
             "column": "total", "to": "gross_total"},
            {"at": "2d", "operation": "rescale_column", "table": "ops.orders",
             "column": "total", "factor": 100},
        ]))


def test_migrations_are_sorted_by_when_they_are_due():
    # So the runner applies them in order whatever order the file
    # listed them in -- and so the self-validation above is checking
    # the sequence that will actually happen.
    pack = load_spec(base([
        {"at": "9d", "operation": "drop_column", "table": "ops.orders",
         "column": "total"},
        {"at": "2d", "operation": "add_column", "table": "ops.orders",
         "column": "note", "type": "text", "length": 20},
    ]))
    assert [migration.at_seconds for migration in pack.migrations] == [2 * 86400, 9 * 86400]


def test_a_migration_at_zero_is_refused():
    with pytest.raises(PackError, match="positive interval"):
        load_spec(base([{"at": 0, "operation": "drop_column", "table": "ops.orders",
                         "column": "total"}]))


def test_an_unknown_operation_lists_the_real_ones():
    with pytest.raises(PackError, match="unknown operation 'reticulate'"):
        load_spec(base([{"at": "1d", "operation": "reticulate", "table": "ops.orders",
                         "column": "total"}]))


def test_a_rescale_needs_a_factor_that_is_not_zero():
    with pytest.raises(PackError, match="needs a `factor`"):
        load_spec(base([{"at": "1d", "operation": "rescale_column",
                         "table": "ops.orders", "column": "total"}]))
    with pytest.raises(PackError, match="erase the column"):
        load_spec(base([{"at": "1d", "operation": "rescale_column",
                         "table": "ops.orders", "column": "total", "factor": 0}]))


def test_a_migration_against_an_undeclared_silo_is_refused():
    with pytest.raises(PackError, match="no schema is declared for silo 'books'"):
        load_spec(base([{"at": "1d", "operation": "drop_column",
                         "table": "books.orders", "column": "total"}]))


def test_an_unrecognised_migration_key_is_refused():
    with pytest.raises(PackError, match="does not understand"):
        load_spec(base([{"at": "1d", "operation": "drop_column", "table": "ops.orders",
                         "column": "total", "wehn": "later"}]))


def test_every_operation_can_be_declared():
    pack = load_spec(base([
        {"at": "1d", "operation": "add_column", "table": "ops.orders",
         "column": "note", "type": "text", "length": 20},
        {"at": "2d", "operation": "rename_column", "table": "ops.orders",
         "column": "note", "to": "memo"},
        {"at": "3d", "operation": "change_column_type", "table": "ops.orders",
         "column": "memo", "type": "text", "length": 200},
        {"at": "4d", "operation": "rescale_column", "table": "ops.orders",
         "column": "total", "factor": "0.01"},
        {"at": "5d", "operation": "drop_column", "table": "ops.orders",
         "column": "memo"},
        {"at": "6d", "operation": "add_table", "table": "ops.credits",
         "columns": {"credit_id": {"type": "text", "length": 64,
                                   "primary_key": True, "nullable": False}}},
        {"at": "7d", "operation": "rename_table", "table": "ops.credits",
         "to": "refunds"},
        {"at": "8d", "operation": "drop_table", "table": "ops.refunds"},
    ]))
    assert len(pack.migrations) == 8
