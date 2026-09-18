"""The retail pack, exercised end to end.

A second business, and deliberately not the same three silos as
field_service: the operational database is MariaDB rather than
PostgreSQL, the second system is a till log rather than an accounting
API, and the file drop carries orders going OUT to a supplier rather
than pay lines coming in.

The point of a second pack is that an engineer meets the same problems
in an unfamiliar shape.
"""

import pathlib

import pytest
from worlds import running_world

PACK = pathlib.Path(__file__).resolve().parent.parent / "packs" / "retail.yaml"


@pytest.fixture(scope="module")
def world(tmp_path_factory, mariadb_binaries, postgres_binaries):
    # Shared: every test here only reads what the fixture set up.
    with running_world(tmp_path_factory.mktemp("shop"), PACK.read_text(), "retail",
                       seed=4, tick_seconds=3600, days=12) as built:
        yield built


def web(world, statement):
    from simulator.relational import fetch_all

    return fetch_all(world.silo("web"), "shop", statement)


def till(world, statement):
    from simulator.relational import fetch_all

    return fetch_all(world.silo("till"), "till", statement)


# -- the shop trades ---------------------------------------------------

@pytest.mark.mariadb
@pytest.mark.postgres
def test_all_three_systems_hold_something(world):
    for table in ("customers", "products", "orders", "order_lines"):
        assert web(world, f"SELECT count(*) FROM `{table}`")[0][0] > 0, table
    assert till(world, "SELECT count(*) FROM transactions")[0][0] > 0
    assert list(world.silo("supplier").path.glob("*.csv"))


@pytest.mark.mariadb
def test_delivery_follows_the_rule_the_shop_wrote_down(world):
    # THE rule conditional values were built for, and the reason the
    # branches are declared rather than hidden in a ternary: a shop
    # changes this threshold and somebody has to be able to find it.
    for floor, ceiling, expected in ((50, None, "0.0000"),
                                     (25, 50, "2.9900"),
                                     (0, 25, "4.9900")):
        clause = f"goods_total >= {floor}" + (f" AND goods_total < {ceiling}"
                                              if ceiling else "")
        rows = web(world, f"SELECT DISTINCT delivery FROM orders WHERE {clause}")
        assert [str(r[0]) for r in rows] == [expected], (clause, rows)


@pytest.mark.mariadb
def test_every_line_describes_the_product_it_priced(world):
    # Three columns from one pick. Three independent draws would give a
    # line with one product's sku, another's name and a third's price,
    # which is the defect `picks` exists to prevent.
    wrong = web(world, """
        SELECT count(*) FROM order_lines l JOIN products p ON p.sku = l.sku
        WHERE l.description <> p.name OR l.unit_price <> p.unit_price
    """)[0][0]
    assert wrong == 0


@pytest.mark.mariadb
def test_an_order_has_more_than_one_line_sometimes(world):
    counts = {row[0] for row in web(world, "SELECT count(*) FROM order_lines "
                                          "GROUP BY order_id")}
    assert len(counts) > 1, counts
    assert max(counts) > 1


@pytest.mark.mariadb
def test_every_line_belongs_to_an_order_that_exists(world):
    # `emitted.web.orders.max.order_id` is how a line names the order
    # just written -- max of a single emitted row IS that row. A first
    # attempt used `first`, which is not an aggregate, loaded cleanly
    # and failed on the first tick.
    orphans = web(world, """
        SELECT count(*) FROM order_lines l
        WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.order_id = l.order_id)
    """)[0][0]
    assert orphans == 0


# -- what a consumer has to notice -------------------------------------

@pytest.mark.mariadb
def test_a_returned_order_keeps_the_total_it_was_placed_for(world):
    # An order that was returned is still an order that was placed for
    # that amount. Zeroing it destroys the record of what was sold, and
    # makes returned orders indistinguishable from tiny ones.
    zeroed = web(world, "SELECT count(*) FROM orders "
                        "WHERE status = 'returned' AND goods_total = 0")[0][0]
    assert zeroed == 0

    # So a consumer totalling revenue without excluding returns is
    # wrong, and nothing in the data warns it.
    everything, kept = web(world, """
        SELECT sum(goods_total), sum(CASE WHEN status <> 'returned'
                                          THEN goods_total ELSE 0 END) FROM orders
    """)[0]
    assert everything > kept > 0


@pytest.mark.mariadb
@pytest.mark.postgres
def test_the_till_knows_about_sales_the_website_does_not(world):
    # A separate machine that syncs nightly, which is why it is a
    # second silo rather than a table in the first. Nothing in the web
    # store records a counter sale, so stock figures drawn from orders
    # alone are wrong by whatever the shop sold over the counter.
    counter = till(world, "SELECT count(*) FROM transactions")[0][0]
    assert counter > 0

    # Every counter sale names a real product, because it picked one.
    skus = {row[0] for row in till(world, "SELECT DISTINCT sku FROM transactions")}
    known = {row[0] for row in web(world, "SELECT sku FROM products")}
    assert skus <= known and skus


@pytest.mark.mariadb
@pytest.mark.postgres
def test_the_supplier_file_holds_a_week_of_counter_sales_only(world):
    import csv

    files = sorted(world.silo("supplier").path.glob("*.csv"))
    assert files
    with files[-1].open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert set(rows[0]) == {"transaction_id", "sku", "quantity", "taken", "rang_at"}

    # A week, not everything. The whole table would grow without bound
    # and a supplier asking "what moved this week" would get the year.
    total = till(world, "SELECT count(*) FROM transactions")[0][0]
    assert len(rows) < total, (len(rows), total)
