"""Tests for effects: what an event changes beyond its own rows.

A sale that does not move stock is not a sale, it is a log entry. What
is worth pinning is that the adjustment lands on the right row, in the
right direction, clamped where the pack says, and that it can see what
the emissions just wrote -- which is the whole reason effects run last.
"""

import textwrap

import pytest
from worlds import running_world, write_pack

from simulator import runner
from simulator.relational import fetch_all
from simulator.spec import PackError, load_spec

STOCK = textwrap.dedent("""
    pack: hardware_shop

    silos:
      shop: {kind: mariadb, database: shop}

    schemas:
      shop:
        tables:
          products:
            columns:
              sku: {type: text, length: 64, primary_key: true, nullable: false}
          inventory:
            columns:
              sku:              {type: text, length: 64, primary_key: true, nullable: false}
              quantity_on_hand: {type: integer, nullable: false}
          sale_items:
            columns:
              sale_item_id: {type: text, length: 64, primary_key: true, nullable: false}
              sku:          {type: text, length: 64, nullable: false}
              quantity:     {type: integer, nullable: false}

    seed:
      - table: shop.products
        count: 4
        columns:
          sku: {generator: id, prefix: sku}
      - table: shop.inventory
        per: shop.products
        columns:
          sku:              {generator: reference, from: subject.sku}
          quantity_on_hand: {generator: constant, value: 5000}

    events:
      sale:
        per: shop.products
        rate_per_hour: 8.0
        emits:
          - table: shop.sale_items
            repeat: {min: 1, max: 2}
            columns:
              sale_item_id: {generator: id, prefix: item}
              sku:          {generator: reference, from: subject.sku}
              quantity:     {generator: weighted, options: {1: 6, 2: 2}}
        effects:
          - adjust: shop.inventory.quantity_on_hand
            by: {generator: expression,
                 expression: "-emitted.shop.sale_items.sum.quantity"}
            where:
              sku: {generator: reference, from: subject.sku}
            floor: 0
    """)


@pytest.fixture
def world(tmp_path, mariadb_binaries):
    with running_world(tmp_path, STOCK, seed=4) as built:
        yield built


def stock_and_sold(world):
    rows = fetch_all(world.silo("shop"), "shop", """
        SELECT i.sku, i.quantity_on_hand,
               coalesce((SELECT sum(s.quantity) FROM sale_items s WHERE s.sku = i.sku), 0)
        FROM inventory i ORDER BY i.sku
    """)
    return [(sku, int(on_hand), int(sold)) for sku, on_hand, sold in rows]


# -- the adjustment --------------------------------------------------

@pytest.mark.mariadb
def test_stock_falls_by_exactly_what_was_sold(world):
    # Started at 5000 so a short run cannot reach the floor, which
    # would otherwise hide an adjustment of the wrong size.
    runner.run(world, total_seconds=43200, tick_seconds=1800)
    results = stock_and_sold(world)
    assert any(sold > 0 for _, _, sold in results), "nothing sold, so nothing was tested"
    for sku, on_hand, sold in results:
        assert on_hand == 5000 - sold, sku


@pytest.mark.mariadb
def test_the_adjustment_lands_only_on_the_matching_row(world):
    # A `where` that matched nothing, or everything, would still leave
    # the totals looking plausible in aggregate.
    runner.run(world, total_seconds=43200, tick_seconds=1800)
    results = stock_and_sold(world)
    assert len({sold for _, _, sold in results}) > 1, "every sku sold the same amount"
    for sku, on_hand, sold in results:
        assert on_hand == 5000 - sold, sku


@pytest.mark.mariadb
def test_the_floor_holds_when_stock_runs_out(tmp_path, mariadb_binaries):
    # Clamped in the database with GREATEST rather than guarded before
    # the write, so it holds even when two adjustments land in one tick.
    source = STOCK.replace("value: 5000", "value: 20")
    built = runner.build(write_pack(tmp_path, source, "scarce"), tmp_path / "scarce", seed=4)
    try:
        runner.seed(built)
        runner.run(built, total_seconds=43200, tick_seconds=1800)
        results = stock_and_sold(built)
        assert all(on_hand >= 0 for _, on_hand, _ in results)
        assert any(on_hand == 0 for _, on_hand, _ in results), "stock never ran out"
        # And the sales kept happening after it did -- the floor clamps
        # the stock, it does not stop the business.
        assert all(sold > 20 for _, _, sold in results)
    finally:
        runner.stop(built)


@pytest.mark.mariadb
def test_an_effect_sees_what_the_emissions_just_wrote(world):
    # THE reason effects run after emissions. The amount to deduct is
    # an aggregate over rows that did not exist when the occurrence
    # began; running effects first would leave nothing to refer to.
    runner.run(world, total_seconds=43200, tick_seconds=1800)
    total_sold = sum(sold for _, _, sold in stock_and_sold(world))
    total_removed = sum(5000 - on_hand for _, on_hand, _ in stock_and_sold(world))
    assert total_sold == total_removed > 0


# -- seeding one table from another ----------------------------------

@pytest.mark.mariadb
def test_a_seed_step_can_key_off_another_table(world):
    # Without `per`, two steps generating ids draw from the same counter
    # and produce different keys -- inventory ended up describing skus
    # that no product had, which is how this was found.
    rows = fetch_all(world.silo("shop"), "shop",
                     "SELECT count(*) FROM inventory i "
                     "WHERE i.sku NOT IN (SELECT sku FROM products)")
    assert rows[0][0] == 0
    counts = fetch_all(world.silo("shop"), "shop",
                       "SELECT (SELECT count(*) FROM products), (SELECT count(*) FROM inventory)")
    assert counts[0][0] == counts[0][1] == 4


# -- declaration, checked at load ------------------------------------

def base(**event) -> dict:
    return {
        "pack": "x",
        "silos": {"shop": {"kind": "mariadb", "database": "shop"}},
        "schemas": {"shop": {"tables": {
            "products": {"columns": {
                "sku": {"type": "text", "length": 64, "primary_key": True,
                        "nullable": False}}},
            "inventory": {"columns": {
                "sku": {"type": "text", "length": 64, "primary_key": True,
                        "nullable": False},
                "quantity_on_hand": {"type": "integer", "nullable": False}}},
        }}},
        "events": {"sale": {
            "rate_per_hour": 1.0, "per": "shop.products",
            "emits": [{"table": "shop.inventory", "columns": {
                "sku": {"generator": "reference", "from": "subject.sku"},
                "quantity_on_hand": {"generator": "constant", "value": 1}}}],
            **event,
        }},
    }


def test_adjusting_a_non_numeric_column_is_refused():
    # Adjusting text is not a thing a business does, and producing SQL
    # the engine rejects would be worse than refusing the pack.
    with pytest.raises(PackError, match="cannot be adjusted"):
        load_spec(base(effects=[{
            "adjust": "shop.inventory.sku",
            "by": {"generator": "constant", "value": 1},
            "where": {"sku": {"generator": "reference", "from": "subject.sku"}}}]))


def test_an_adjustment_without_a_where_is_refused():
    # It would silently move every row in the table.
    with pytest.raises(PackError, match="needs a `where`"):
        load_spec(base(effects=[{
            "adjust": "shop.inventory.quantity_on_hand",
            "by": {"generator": "constant", "value": -1}}]))


def test_an_adjustment_needs_a_by():
    with pytest.raises(PackError, match="needs a `by`"):
        load_spec(base(effects=[{
            "adjust": "shop.inventory.quantity_on_hand",
            "where": {"sku": {"generator": "reference", "from": "subject.sku"}}}]))


def test_an_effect_cannot_refer_to_the_row_being_built():
    # Effects run after every emission has finished its row, so the
    # context's own row is empty and `row` refers to nothing.
    with pytest.raises(PackError, match="only `subject` and `emitted` are available"):
        load_spec(base(effects=[{
            "adjust": "shop.inventory.quantity_on_hand",
            "by": {"generator": "reference", "from": "row.quantity"},
            "where": {"sku": {"generator": "reference", "from": "subject.sku"}}}]))


def test_a_where_column_that_does_not_exist_is_refused():
    with pytest.raises(PackError, match="has no column 'barcode'"):
        load_spec(base(effects=[{
            "adjust": "shop.inventory.quantity_on_hand",
            "by": {"generator": "constant", "value": -1},
            "where": {"barcode": {"generator": "reference", "from": "subject.sku"}}}]))


def test_a_malformed_adjust_target_is_refused():
    with pytest.raises(PackError, match="silo.table.column"):
        load_spec(base(effects=[{
            "adjust": "inventory.quantity_on_hand",
            "by": {"generator": "constant", "value": -1},
            "where": {"sku": {"generator": "constant", "value": "x"}}}]))


def test_an_unrecognised_effect_key_is_refused():
    with pytest.raises(PackError, match="does not understand"):
        load_spec(base(effects=[{
            "adjust": "shop.inventory.quantity_on_hand",
            "by": {"generator": "constant", "value": -1},
            "wheer": {},
            "where": {"sku": {"generator": "constant", "value": "x"}}}]))


def test_a_seed_step_with_per_may_write_several_rows_per_subject():
    # This used to be refused -- "a step with `per` writes one row per
    # subject, so it takes no count" -- and that rule made a join table
    # inexpressible, because a technician has several skills and not
    # one. `count` alongside `per` now means rows PER SUBJECT.
    pack = load_spec({
        "pack": "x",
        "silos": {"shop": {"kind": "mariadb", "database": "shop"}},
        "schemas": {"shop": {"tables": {"products": {"columns": {
            "sku": {"type": "text", "length": 64, "primary_key": True,
                    "nullable": False}}}}}},
        "seed": [{"table": "shop.products", "per": "shop.products", "count": 3,
                  "columns": {"sku": {"generator": "id", "prefix": "s"}}}],
    })
    step = pack.seed[0]
    assert step.per == "shop.products"
    assert step.count == 3
