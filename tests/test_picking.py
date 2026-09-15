"""Tests for choosing a row to build another row from.

A sale picks a product; a work order picks a technician. Declared
above the columns rather than inside one, because a generator returns
a single value -- two pick generators in the same row would choose two
DIFFERENT products, and a line needs its sku and its price to come
from the same one.
"""

import textwrap

import pytest

from simulator import runner
from simulator.relational import fetch_all
from simulator.spec import PackError, load_pack, load_spec

SHOP = textwrap.dedent("""
    pack: picking_shop

    silos:
      shop: {kind: mariadb, database: shop}

    schemas:
      shop:
        tables:
          products:
            columns:
              sku:        {type: text, length: 64, primary_key: true, nullable: false}
              name:       {type: text, length: 120, nullable: false}
              unit_price: {type: decimal, precision: 19, scale: 4, nullable: false}
          tills:
            columns:
              till_id: {type: text, length: 64, primary_key: true, nullable: false}
          sale_items:
            columns:
              sale_item_id: {type: text, length: 64, primary_key: true, nullable: false}
              sale_id:      {type: text, length: 64, nullable: false}
              sku:          {type: text, length: 64, nullable: false}
              name:         {type: text, length: 120, nullable: false}
              quantity:     {type: integer, nullable: false}
              unit_price:   {type: decimal, precision: 19, scale: 4, nullable: false}
              line_total:   {type: decimal, precision: 19, scale: 4, nullable: false}

    seed:
      - table: shop.tills
        count: 2
        columns:
          till_id: {generator: id, prefix: till}
      - table: shop.products
        count: 12
        columns:
          sku:        {generator: id, prefix: sku}
          name:       {generator: template, pattern: "Part {sku}"}
          unit_price: {generator: decimal, min: 2, max: 240, scale: 4}

    events:
      sale:
        per: shop.tills
        rate_per_hour: 2.0
        emits:
          - table: shop.sale_items
            repeat: {min: 1, max: 4}
            picks: [shop.products]
            columns:
              sale_item_id: {generator: id, prefix: item}
              sale_id:      {generator: occurrence_id, prefix: sale}
              sku:          {generator: reference, from: picked.products.sku}
              name:         {generator: reference, from: picked.products.name}
              quantity:     {generator: weighted, options: {1: 6, 2: 2, 3: 1}}
              unit_price:   {generator: reference, from: picked.products.unit_price}
              line_total:   {generator: expression, expression: "quantity * unit_price"}
    """)


@pytest.fixture
def world(tmp_path, mariadb_binaries):
    path = tmp_path / "shop.yaml"
    path.write_text(SHOP)
    built = runner.build(load_pack(path), tmp_path / "var", seed=5)
    runner.seed(built)
    runner.run(built, total_seconds=3 * 86400, tick_seconds=1800)
    try:
        yield built
    finally:
        runner.stop(built)


def query(world, statement):
    return fetch_all(world.silo("shop"), "shop", statement)


# -- what picking buys -------------------------------------------------

@pytest.mark.mariadb
def test_every_column_comes_from_the_same_picked_row(world):
    # THE reason this is an emission-level declaration. If each column
    # picked independently, a line would carry one product's sku, a
    # second's name and a third's price -- and every row would still
    # look perfectly plausible.
    wrong = query(world, """
        SELECT count(*) FROM sale_items i
        JOIN products p ON p.sku = i.sku
        WHERE i.unit_price <> p.unit_price OR i.name <> p.name
    """)[0][0]
    assert wrong == 0
    assert query(world, "SELECT count(*) FROM sale_items")[0][0] > 50


@pytest.mark.mariadb
def test_a_line_only_ever_names_a_product_that_exists(world):
    orphans = query(world, "SELECT count(*) FROM sale_items "
                           "WHERE sku NOT IN (SELECT sku FROM products)")[0][0]
    assert orphans == 0


@pytest.mark.mariadb
def test_picking_happens_per_row_not_per_occurrence(world):
    # A sale with three lines is three different products. Picking once
    # for the whole occurrence would make every line of every sale the
    # same item, which looks like data and is not.
    biggest = query(world, """
        SELECT count(*), count(DISTINCT sku) FROM sale_items
        GROUP BY sale_id ORDER BY count(*) DESC LIMIT 1
    """)[0]
    assert biggest[0] > 1, "no multi-line sale to judge"
    assert biggest[1] > 1, "every line of a sale was the same product"


@pytest.mark.mariadb
def test_the_whole_catalogue_gets_sold(world):
    # A pick that always returned the first row would show one product
    # and nothing else.
    distinct = query(world, "SELECT count(DISTINCT sku) FROM sale_items")[0][0]
    assert distinct >= 10, distinct


@pytest.mark.mariadb
def test_a_picked_price_is_frozen_into_the_line(world):
    # The business point: a line's unit price is the product's price at
    # that moment, copied. line_total is computed from it, so the two
    # must agree even though nothing recomputes them together.
    wrong = query(world, "SELECT count(*) FROM sale_items "
                         "WHERE line_total <> quantity * unit_price")[0][0]
    assert wrong == 0


# -- declaration, checked at load --------------------------------------

def base(emission) -> dict:
    return {
        "pack": "x",
        "silos": {"shop": {"kind": "mariadb", "database": "shop"},
                  "other": {"kind": "postgresql", "database": "other"}},
        "schemas": {
            "shop": {"tables": {
                "products": {"columns": {
                    "sku": {"type": "text", "length": 64, "primary_key": True,
                            "nullable": False}}},
                "lines": {"columns": {
                    "line_id": {"type": "text", "length": 64, "primary_key": True,
                                "nullable": False},
                    "sku": {"type": "text", "length": 64, "nullable": False}}},
            }},
            "other": {"tables": {"products": {"columns": {
                "sku": {"type": "text", "length": 64, "primary_key": True,
                        "nullable": False}}}}},
        },
        "events": {"sale": {"rate_per_hour": 1.0, "emits": [emission]}},
    }


def line(**changes) -> dict:
    return {"table": "shop.lines", "picks": ["shop.products"],
            "columns": {"line_id": {"generator": "id", "prefix": "l"},
                        "sku": {"generator": "reference",
                                "from": "picked.products.sku"}}, **changes}


def test_a_pick_loads():
    pack = load_spec(base(line()))
    assert pack.events[0].emissions[0].picks == {"products": "shop.products"}


def test_referring_to_a_pick_that_was_not_declared_is_refused():
    with pytest.raises(PackError, match="does not pick from 'widgets'"):
        load_spec(base(line(columns={
            "line_id": {"generator": "id", "prefix": "l"},
            "sku": {"generator": "reference", "from": "picked.widgets.sku"}})))


def test_referring_to_a_column_the_picked_table_lacks_is_refused():
    with pytest.raises(PackError, match=r"has columns \['sku'\]"):
        load_spec(base(line(columns={
            "line_id": {"generator": "id", "prefix": "l"},
            "sku": {"generator": "reference", "from": "picked.products.barcode"}})))


def test_picking_without_declaring_it_is_still_refused():
    # The namespace existed from the beginning and nothing could reach
    # it. Reaching it now requires saying what is being picked.
    with pytest.raises(PackError, match="does not pick from"):
        load_spec(base({"table": "shop.lines", "columns": {
            "line_id": {"generator": "id", "prefix": "l"},
            "sku": {"generator": "reference", "from": "picked.products.sku"}}}))


def test_two_picks_that_would_shadow_each_other_are_refused():
    # A pack refers to a pick by its bare table name, so two tables
    # called `products` in different silos would silently shadow.
    with pytest.raises(PackError, match="would shadow each other"):
        load_spec(base(line(picks=["shop.products", "other.products"])))


def test_picking_a_table_that_does_not_exist_is_refused():
    with pytest.raises(PackError, match="has no table 'widgets'"):
        load_spec(base(line(picks=["shop.widgets"])))


def test_a_malformed_pick_is_refused():
    with pytest.raises(PackError, match="silo.table"):
        load_spec(base(line(picks=["products"])))
    with pytest.raises(PackError, match="list of silo.table"):
        load_spec(base(line(picks="shop.products")))


def test_a_later_emission_cannot_use_an_earlier_ones_pick():
    # Picks belong to the emission that declared them: a later emission
    # referring to one would be reading a row that is no longer being
    # built.
    spec = base(line())
    spec["events"]["sale"]["emits"].append({
        "table": "shop.lines",
        "columns": {"line_id": {"generator": "id", "prefix": "m"},
                    "sku": {"generator": "reference", "from": "picked.products.sku"}}})
    with pytest.raises(PackError, match="does not pick from"):
        load_spec(spec)


@pytest.mark.mariadb
def test_picking_from_an_empty_table_says_why(tmp_path, mariadb_binaries):
    # Reference data has to be seeded before anything can choose from
    # it, and the error should say so rather than surface as an
    # IndexError from inside a tick.
    #
    # A dedicated pack rather than surgery on SHOP: editing an already
    # dedented string by replacement has silently produced a pack that
    # tested nothing twice in this codebase already.
    from simulator.event import EventError

    source = textwrap.dedent("""
        pack: empty_shelves

        silos:
          shop: {kind: mariadb, database: shop}

        schemas:
          shop:
            tables:
              products:
                columns:
                  sku: {type: text, length: 64, primary_key: true, nullable: false}
              tills:
                columns:
                  till_id: {type: text, length: 64, primary_key: true, nullable: false}
              lines:
                columns:
                  line_id: {type: text, length: 64, primary_key: true, nullable: false}
                  sku:     {type: text, length: 64, nullable: false}

        seed:
          - table: shop.tills
            count: 2
            columns:
              till_id: {generator: id, prefix: till}

        events:
          sale:
            per: shop.tills
            rate_per_hour: 20.0
            emits:
              - table: shop.lines
                picks: [shop.products]
                columns:
                  line_id: {generator: id, prefix: l}
                  sku:     {generator: reference, from: picked.products.sku}
        """)
    path = tmp_path / "empty.yaml"
    path.write_text(source)
    world = runner.build(load_pack(path), tmp_path / "var", seed=5)
    try:
        runner.seed(world)
        # products was never seeded, so the first sale has nothing to
        # pick.
        assert fetch_all(world.silo("shop"), "shop",
                         "SELECT count(*) FROM products")[0][0] == 0
        with pytest.raises(EventError, match="no rows"):
            runner.run(world, total_seconds=86400, tick_seconds=1800)
    finally:
        runner.stop(world)
