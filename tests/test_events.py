"""Tests for events: what happens, how often, and what it writes.

The end-to-end cases run a simulated week against a real engine,
because the properties worth checking here -- that a sale totals the
lines it just wrote, that trade follows the clock, that tick size does
not change behaviour -- are all about what ends up in a database.
"""

import textwrap

import pytest

from simulator import runner
from simulator.relational import count_rows, fetch_all
from simulator.spec import PackError, load_pack, load_spec

SHOP = textwrap.dedent("""
    pack: hardware_shop

    silos:
      shop: {kind: mariadb, database: shop}

    schemas:
      shop:
        tables:
          products:
            columns:
              sku:        {type: text, length: 64, primary_key: true, nullable: false}
              unit_price: {type: decimal, precision: 19, scale: 4, nullable: false}
          sales:
            columns:
              sale_id: {type: text, length: 64, primary_key: true, nullable: false}
              sku:     {type: text, length: 64, nullable: false}
              sold_at: {type: timestamp, nullable: false}
              total:   {type: decimal, precision: 19, scale: 4, nullable: false}
          sale_items:
            columns:
              sale_item_id: {type: text, length: 64, primary_key: true, nullable: false}
              sale_id:      {type: text, length: 64, nullable: false}
              sku:          {type: text, length: 64, nullable: false}
              quantity:     {type: integer, nullable: false}
              unit_price:   {type: decimal, precision: 19, scale: 4, nullable: false}
              line_total:   {type: decimal, precision: 19, scale: 4, nullable: false}

    curves:
      trading_day: [0.02,0.01,0.01,0.01,0.02,0.08,0.25,0.55,0.80,0.95,1.00,1.10,
                    1.35,1.20,1.00,1.05,1.20,1.40,1.25,0.90,0.55,0.30,0.12,0.05]

    seed:
      - table: shop.products
        count: 8
        columns:
          sku:        {generator: id, prefix: sku}
          unit_price: {generator: decimal, min: 2, max: 60, scale: 4}

    events:
      sale:
        per: shop.products
        rate_per_hour: 4.0
        curve: trading_day
        emits:
          - table: shop.sale_items
            repeat: {min: 1, max: 3}
            columns:
              sale_item_id: {generator: id, prefix: item}
              sale_id:      {generator: occurrence_id, prefix: sale}
              sku:          {generator: reference, from: subject.sku}
              quantity:     {generator: weighted, options: {1: 6, 2: 2, 3: 1}}
              unit_price:   {generator: reference, from: subject.unit_price}
              line_total:   {generator: expression, expression: "quantity * unit_price"}
          - table: shop.sales
            columns:
              sale_id: {generator: occurrence_id, prefix: sale}
              sku:     {generator: reference, from: subject.sku}
              sold_at: {generator: now}
              total:   {generator: reference, from: "emitted.shop.sale_items.sum.line_total"}
    """)


def write_pack(tmp_path, source=SHOP, name="shop"):
    path = tmp_path / f"{name}.yaml"
    path.write_text(source)
    return load_pack(path)


@pytest.fixture
def world(tmp_path, mariadb_binaries):
    built = runner.build(write_pack(tmp_path), tmp_path / "var", seed=11)
    runner.seed(built)
    try:
        yield built
    finally:
        runner.stop(built)


# -- a simulated week ------------------------------------------------

@pytest.mark.mariadb
def test_a_week_of_trading_produces_rows(world):
    written = runner.run(world, total_seconds=2 * 86400, tick_seconds=1800)
    assert written > 0
    sales = count_rows(world.silo("shop"), "shop", "sales")
    items = count_rows(world.silo("shop"), "shop", "sale_items")
    assert sales > 50
    # Between one and three lines per sale, so items must exceed sales
    # without reaching three times it on any realistic draw.
    assert sales < items < sales * 3


@pytest.mark.mariadb
def test_a_sale_totals_the_lines_it_just_wrote(world):
    # THE reason one EvaluationContext is built per occurrence rather
    # than per emission: `emitted` has to mean "what this event has
    # written so far", or a sale could not total its own lines.
    #
    # Checked PER SALE, which only became expressible once an id could
    # be issued once per occurrence. Before that a line could not carry
    # its sale's id -- the sale has to be emitted after the lines it
    # totals, so its id did not exist while they were being built --
    # and the strongest available invariant was an aggregate over the
    # whole run, which would not have caught totals attached to the
    # wrong sales.
    runner.run(world, total_seconds=86400, tick_seconds=1800)
    mismatched = fetch_all(world.silo("shop"), "shop", """
        SELECT s.sale_id, s.total, sum(i.line_total)
        FROM sales s JOIN sale_items i ON i.sale_id = s.sale_id
        GROUP BY s.sale_id, s.total
        HAVING s.total <> sum(i.line_total)
    """)
    assert mismatched == []
    totals = fetch_all(world.silo("shop"), "shop", "SELECT total FROM sales LIMIT 20")
    assert all(total > 0 for (total,) in totals)


@pytest.mark.mariadb
def test_every_line_belongs_to_a_sale_that_exists(world):
    # The link the occurrence id buys, asserted directly: no orphans in
    # either direction.
    runner.run(world, total_seconds=86400, tick_seconds=1800)
    orphans = fetch_all(world.silo("shop"), "shop",
                        "SELECT count(*) FROM sale_items i "
                        "WHERE i.sale_id NOT IN (SELECT sale_id FROM sales)")
    assert orphans[0][0] == 0
    childless = fetch_all(world.silo("shop"), "shop",
                          "SELECT count(*) FROM sales s "
                          "WHERE s.sale_id NOT IN (SELECT sale_id FROM sale_items)")
    assert childless[0][0] == 0


@pytest.mark.mariadb
def test_each_occurrence_gets_its_own_id(world):
    runner.run(world, total_seconds=86400, tick_seconds=1800)
    counts = fetch_all(world.silo("shop"), "shop",
                       "SELECT count(*), count(DISTINCT sale_id) FROM sales")[0]
    assert counts[0] == counts[1]


@pytest.mark.mariadb
def test_a_line_takes_the_subject_price_not_a_new_one(world):
    # The business point: a line's unit price is the product's price at
    # that moment, copied. Every line must match its product exactly.
    runner.run(world, total_seconds=86400, tick_seconds=1800)
    wrong = fetch_all(world.silo("shop"), "shop", """
        SELECT i.sale_item_id FROM sale_items i
        JOIN products p ON p.sku = i.sku
        WHERE i.unit_price <> p.unit_price
    """)
    assert wrong == []


@pytest.mark.mariadb
def test_line_totals_are_quantity_times_price(world):
    runner.run(world, total_seconds=86400, tick_seconds=1800)
    wrong = fetch_all(world.silo("shop"), "shop",
                      "SELECT sale_item_id FROM sale_items "
                      "WHERE line_total <> quantity * unit_price")
    assert wrong == []


@pytest.mark.mariadb
def test_trade_follows_the_clock(world):
    # A flat arrival rate is the clearest tell that operational data
    # was generated, so the curve is worth asserting on directly.
    runner.run(world, total_seconds=2 * 86400, tick_seconds=1800)
    rows = fetch_all(world.silo("shop"), "shop",
                     "SELECT substr(sold_at, 12, 2) AS hr, count(*) FROM sales GROUP BY hr")
    by_hour = {hour: count for hour, count in rows}
    busy = sum(by_hour.get(f"{hour:02d}", 0) for hour in (12, 13, 16, 17))
    quiet = sum(by_hour.get(f"{hour:02d}", 0) for hour in (1, 2, 3, 4))
    assert busy > quiet * 5


@pytest.mark.mariadb
def test_nothing_happens_in_a_zero_length_tick(world):
    before = count_rows(world.silo("shop"), "shop", "sales")
    runner.tick(world, 0)
    assert count_rows(world.silo("shop"), "shop", "sales") == before


@pytest.mark.mariadb
def test_the_clock_advances_with_the_run(world):
    start = world.clock.now()
    runner.run(world, total_seconds=3600, tick_seconds=900)
    assert (world.clock.now() - start).total_seconds() == 3600


@pytest.mark.mariadb
def test_events_see_the_time_they_happen_at(tmp_path, mariadb_binaries):
    # The clock advances BEFORE events fire, so an event sees the time
    # it is happening at rather than the time the interval started.
    #
    # A first version of this asserted only that the clock had moved by
    # the right amount, which is true whichever order those two steps
    # happen in -- so it passed against the reversed implementation.
    # The ordering is visible only in what the events WRITE: a row
    # timestamped at the start of the interval means the clock moved
    # last.
    source = SHOP.replace("rate_per_hour: 4.0", "rate_per_hour: 20")
    built = runner.build(write_pack(tmp_path, source, "busy"), tmp_path / "busy", seed=3)
    try:
        runner.seed(built)
        start = built.clock.now()
        runner.tick(built, 3600)
        stamps = fetch_all(built.silo("shop"), "shop", "SELECT DISTINCT sold_at FROM sales")
        assert stamps, "the rate was not high enough to guarantee a sale"
        # Compared naive, because MariaDB's DATETIME carries no offset
        # and hands one back without tzinfo -- which is the documented
        # trade the dialect makes rather than a defect, since its
        # TIMESTAMP converts through the session time zone in both
        # directions and expires in 2038.
        assert all(stamp > start.replace(tzinfo=None) for (stamp,) in stamps)
    finally:
        runner.stop(built)


@pytest.mark.mariadb
def test_behaviour_does_not_depend_on_tick_size(tmp_path, mariadb_binaries):
    # Rates are per hour and arrivals are per interval, so slicing time
    # more finely must not change how much trade happens. A pack that
    # derived anything from a tick COUNT would break this.
    def sales_at(tick_seconds, name):
        built = runner.build(write_pack(tmp_path), tmp_path / name, seed=5)
        try:
            runner.seed(built)
            runner.run(built, total_seconds=7 * 86400, tick_seconds=tick_seconds)
            return count_rows(built.silo("shop"), "shop", "sales")
        finally:
            runner.stop(built)

    fine = sales_at(600, "fine")
    coarse = sales_at(3600, "coarse")
    assert fine == pytest.approx(coarse, rel=0.3)


@pytest.mark.mariadb
def test_arrivals_scale_with_the_number_of_subjects(tmp_path, mariadb_binaries):
    # Per subject, not per world. A shop that doubles its product range
    # should sell more; a world-level rate would keep the total flat and
    # silently rescale each product.
    def sales_with(products, name):
        source = SHOP.replace("count: 8", f"count: {products}")
        built = runner.build(write_pack(tmp_path, source, name), tmp_path / name, seed=5)
        try:
            runner.seed(built)
            runner.run(built, total_seconds=2 * 86400, tick_seconds=1800)
            return count_rows(built.silo("shop"), "shop", "sales")
        finally:
            runner.stop(built)

    assert sales_with(24, "many") > sales_with(8, "few") * 2


def test_a_run_needs_a_positive_tick(tmp_path):
    from simulator.clock import SimulatedClock, utc
    from simulator.ports import PortRegistry
    from simulator.rng import RandomSource
    from simulator.world import World

    world = World(pack=write_pack(tmp_path), clock=SimulatedClock(start=utc(2026, 1, 1)),
                  rng=RandomSource(1), silos={},
                  ports=PortRegistry(path=tmp_path / "p.json", ports={}))
    with pytest.raises(ValueError, match="must be positive"):
        runner.run(world, 3600, tick_seconds=0)


# -- declaration, checked at load ------------------------------------

def base_spec(**event) -> dict:
    return {
        "pack": "x",
        "silos": {"shop": {"kind": "mariadb", "database": "shop"}},
        "schemas": {"shop": {"tables": {
            "products": {"columns": {
                "sku": {"type": "text", "length": 64, "primary_key": True,
                        "nullable": False}}},
            "sales": {"columns": {
                "sale_id": {"type": "text", "length": 64, "primary_key": True,
                            "nullable": False},
                "sku": {"type": "text", "length": 64, "nullable": False}}},
        }}},
        "events": {"sale": event},
    }


def test_an_event_needs_a_rate():
    with pytest.raises(PackError, match="needs a rate_per_hour"):
        load_spec(base_spec(emits=[{"table": "shop.sales", "columns": {}}]))


def test_an_event_needs_an_emission():
    with pytest.raises(PackError, match="at least one emission"):
        load_spec(base_spec(rate_per_hour=1.0))


def test_a_subject_reference_without_a_per_is_refused():
    with pytest.raises(PackError, match="this event has no `per`"):
        load_spec(base_spec(rate_per_hour=1.0, emits=[{
            "table": "shop.sales",
            "columns": {
                "sale_id": {"generator": "id", "prefix": "s"},
                "sku": {"generator": "reference", "from": "subject.sku"}},
        }]))


def test_a_subject_column_that_does_not_exist_is_refused():
    with pytest.raises(PackError, match="the subject table has columns"):
        load_spec(base_spec(rate_per_hour=1.0, per="shop.products", emits=[{
            "table": "shop.sales",
            "columns": {
                "sale_id": {"generator": "id", "prefix": "s"},
                "sku": {"generator": "reference", "from": "subject.barcode"}},
        }]))


def test_a_forward_reference_to_a_later_emission_is_refused():
    # Referring to a table emitted LATER in the same event would fail
    # mid-run with an empty aggregate. The order of emissions is known
    # at load, so it is caught here.
    with pytest.raises(PackError, match="no earlier emission"):
        load_spec(base_spec(rate_per_hour=1.0, per="shop.products", emits=[{
            "table": "shop.sales",
            "columns": {
                "sale_id": {"generator": "id", "prefix": "s"},
                "sku": {"generator": "reference",
                        "from": "emitted.shop.sale_items.sum.line_total"}},
        }]))


def test_an_unknown_curve_lists_the_declared_ones():
    with pytest.raises(PackError, match="no curve called 'weekend'"):
        load_spec(base_spec(rate_per_hour=1.0, curve="weekend", emits=[{
            "table": "shop.sales",
            "columns": {"sale_id": {"generator": "id", "prefix": "s"},
                        "sku": {"generator": "constant", "value": "x"}}}]))


def test_a_per_table_that_does_not_exist_is_refused():
    with pytest.raises(PackError, match="has no table 'widgets'"):
        load_spec(base_spec(rate_per_hour=1.0, per="shop.widgets", emits=[{
            "table": "shop.sales",
            "columns": {"sale_id": {"generator": "id", "prefix": "s"},
                        "sku": {"generator": "constant", "value": "x"}}}]))


def test_every_non_null_column_must_be_generated():
    with pytest.raises(PackError, match=r"non-null column\(s\) \['sku'\]"):
        load_spec(base_spec(rate_per_hour=1.0, emits=[{
            "table": "shop.sales",
            "columns": {"sale_id": {"generator": "id", "prefix": "s"}}}]))


@pytest.mark.parametrize("repeat", [0, -1, {"min": 3, "max": 1}, "lots"])
def test_a_malformed_repeat_is_refused(repeat):
    with pytest.raises(PackError):
        load_spec(base_spec(rate_per_hour=1.0, repeat=repeat, emits=[{
            "table": "shop.sales", "repeat": repeat,
            "columns": {"sale_id": {"generator": "id", "prefix": "s"},
                        "sku": {"generator": "constant", "value": "x"}}}]))


def test_an_unrecognised_event_key_is_refused():
    with pytest.raises(PackError, match="does not understand"):
        load_spec(base_spec(rate_per_hour=1.0, wehn="always", emits=[{
            "table": "shop.sales",
            "columns": {"sale_id": {"generator": "id", "prefix": "s"},
                        "sku": {"generator": "constant", "value": "x"}}}]))


def test_a_reference_to_an_earlier_emission_is_accepted():
    # The positive that gives the forward-reference test its meaning.
    # Without this, a loader that refused EVERY `emitted` reference
    # would satisfy the negative case perfectly -- which is exactly what
    # a control found when the emission bookkeeping was removed.
    pack = load_spec(base_spec(rate_per_hour=1.0, per="shop.products", emits=[
        {"table": "shop.sales",
         "columns": {"sale_id": {"generator": "id", "prefix": "s"},
                     "sku": {"generator": "reference", "from": "subject.sku"}}},
        {"table": "shop.products",
         "columns": {"sku": {"generator": "reference",
                             "from": "emitted.shop.sales.count.sale_id"}}},
    ]))
    assert len(pack.events[0].emissions) == 2
