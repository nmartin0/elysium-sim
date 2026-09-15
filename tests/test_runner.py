"""Tests for building a world from a pack and seeding it.

The end-to-end cases run against real engines, because this layer's
whole job is to make a YAML file become running databases with rows in
them, and a mock would only confirm that the code calls the methods the
mock expects.
"""

import textwrap

import pytest

from simulator import runner
from simulator.relational import count_rows, fetch_all
from simulator.silo import SiloError
from simulator.spec import PackError, load_pack, load_spec

FIELD_SERVICE = textwrap.dedent("""
    pack: field_service
    description: A small plumbing and heating firm.

    silos:
      dispatch: {kind: postgresql, database: dispatch}
      shop:     {kind: mariadb, database: shop}
      ledger:   {kind: sqlite}
      payroll:  {kind: filedrop}
      books:    {kind: rest}

    schemas:
      dispatch:
        tables:
          customers:
            columns:
              customer_id:  {type: text, length: 64, primary_key: true, nullable: false}
              name:         {type: text, length: 200, nullable: false}
              credit_limit: {type: decimal, precision: 19, scale: 4, nullable: false}
              joined_on:    {type: date, nullable: false}
      shop:
        tables:
          products:
            columns:
              sku:        {type: text, length: 64, primary_key: true, nullable: false}
              name:       {type: text, length: 200, nullable: false}
              unit_price: {type: decimal, precision: 19, scale: 4, nullable: false}
              vat_price:  {type: decimal, precision: 19, scale: 4, nullable: false}

    seed:
      - table: dispatch.customers
        count: 25
        columns:
          customer_id:  {generator: id, prefix: cust}
          name:         {generator: choice, options: [Okafor, Feldman, Barros]}
          credit_limit: {generator: decimal, min: 500, max: 5000, scale: 4}
          joined_on:    {generator: now}
      - table: shop.products
        count: 12
        columns:
          sku:        {generator: id, prefix: sku}
          name:       {generator: template, pattern: "Part {sku}"}
          unit_price: {generator: decimal, min: 2, max: 240, scale: 4}
          vat_price:  {generator: expression, expression: "unit_price * 1.2"}
    """)


@pytest.fixture
def pack(tmp_path):
    path = tmp_path / "field_service.yaml"
    path.write_text(FIELD_SERVICE)
    return load_pack(path)


@pytest.fixture
def world(pack, tmp_path, postgres_binaries, mariadb_binaries):
    built = runner.build(pack, tmp_path / "var", seed=7)
    try:
        yield built
    finally:
        runner.stop(built)


# -- building --------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.mariadb
def test_five_silos_of_four_kinds_all_come_up(world):
    # What a small business really looks like: a database, another
    # database on a different engine, an embedded file, a folder and
    # an API -- none of them knowing about the others.
    assert set(world.silos) == {"dispatch", "shop", "ledger", "payroll", "books"}
    for name, silo in world.silos.items():
        assert silo.is_reachable(), name


@pytest.mark.postgres
@pytest.mark.mariadb
def test_ports_are_allocated_only_for_silos_that_listen(world):
    # sqlite is a file and filedrop is a folder; neither listens.
    assert set(world.ports.ports) == {"dispatch", "shop", "books"}
    assert len(set(world.ports.ports.values())) == 3


@pytest.mark.postgres
@pytest.mark.mariadb
def test_connections_name_the_declared_database_not_the_default(world):
    # Left alone, the relational silos answer with their MAINTENANCE
    # database -- `postgres` and `mysql` -- which exists, accepts
    # connections, and holds none of the business's data. A consumer
    # following that would connect successfully to the wrong place and
    # find nothing, which is worse than failing to connect.
    connections = world.connections()
    assert connections["dispatch"].details["database"] == "dispatch"
    assert connections["shop"].details["database"] == "shop"


@pytest.mark.postgres
@pytest.mark.mariadb
def test_every_connection_summary_says_where_to_connect(world):
    # A summary that omits the location is worse than none: someone
    # reads it, sees a silo listed, and has no idea where it is. The
    # REST silo answered with a bare "rest" before this was fixed.
    for name, connection in world.connections().items():
        summary = connection.summary()
        assert summary != connection.kind, name
        assert len(summary) > len(connection.kind), name
    assert world.connections()["books"].summary().startswith("http://127.0.0.1:")


@pytest.mark.postgres
@pytest.mark.mariadb
def test_schemas_are_applied_and_verified_against_the_engines(world):
    assert count_rows(world.silo("dispatch"), "dispatch", "customers") == 0
    assert count_rows(world.silo("shop"), "shop", "products") == 0


# -- seeding ---------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.mariadb
def test_seeding_writes_what_the_pack_declares(world):
    assert runner.seed(world) == {"dispatch.customers": 25, "shop.products": 12}
    assert count_rows(world.silo("dispatch"), "dispatch", "customers") == 25
    assert count_rows(world.silo("shop"), "shop", "products") == 12


@pytest.mark.postgres
@pytest.mark.mariadb
def test_a_later_column_can_refer_to_an_earlier_one(world):
    # The reason columns are generated in DECLARED order: each value
    # goes into the context's row before the next generator runs.
    runner.seed(world)
    rows = fetch_all(world.silo("shop"), "shop",
                     "SELECT sku, name, unit_price, vat_price FROM products ORDER BY sku")
    for sku, name, unit_price, vat_price in rows:
        assert name == f"Part {sku}"
        assert vat_price == (unit_price * 12 / 10).quantize(unit_price)


@pytest.mark.postgres
@pytest.mark.mariadb
def test_generated_ids_are_readable_and_sequential(world):
    runner.seed(world)
    rows = fetch_all(world.silo("dispatch"), "dispatch",
                     "SELECT customer_id FROM customers ORDER BY customer_id")
    assert [row[0] for row in rows][:3] == ["cust_000001", "cust_000002", "cust_000003"]


@pytest.mark.postgres
@pytest.mark.mariadb
def test_money_arrives_as_exact_decimals(world):
    runner.seed(world)
    total = fetch_all(world.silo("dispatch"), "dispatch",
                      "SELECT credit_limit FROM customers ORDER BY customer_id LIMIT 1")[0][0]
    assert total.as_tuple().exponent == -4


@pytest.mark.postgres
@pytest.mark.mariadb
def test_the_same_seed_produces_the_same_world(pack, tmp_path, postgres_binaries,
                                               mariadb_binaries):
    def run_once(name):
        built = runner.build(pack, tmp_path / name, seed=3)
        try:
            runner.seed(built)
            return fetch_all(built.silo("shop"), "shop",
                             "SELECT sku, unit_price FROM products ORDER BY sku")
        finally:
            runner.stop(built)

    assert run_once("a") == run_once("b")


@pytest.mark.postgres
@pytest.mark.mariadb
def test_changing_one_seed_step_does_not_shift_another(tmp_path, postgres_binaries,
                                                       mariadb_binaries):
    # THE property a stream per table buys, and it is not determinism --
    # a single shared stream is perfectly deterministic too, which is
    # why the reproducibility test above passes either way. What this
    # checks is INDEPENDENCE: adding a column to one seed step must
    # leave every other step byte-identical, because otherwise "change
    # one thing and see what that one thing did" stops working.
    def products_when_customers(name, count):
        source = FIELD_SERVICE.replace("count: 25", f"count: {count}")
        path = tmp_path / f"{name}.yaml"
        path.write_text(source)
        built = runner.build(load_pack(path), tmp_path / name, seed=5)
        try:
            runner.seed(built)
            return fetch_all(built.silo("shop"), "shop",
                             "SELECT sku, unit_price FROM products ORDER BY sku")
        finally:
            runner.stop(built)

    # Seeding 40 customers instead of 25 draws fifteen rows' worth of
    # extra random values. The products table must not notice.
    assert products_when_customers("small", 25) == products_when_customers("large", 40)


# -- the world, without a server -------------------------------------

def test_a_world_holds_one_clock_and_one_set_of_streams(pack, tmp_path):
    # Not built, just constructed: two subsystems holding different
    # clocks is how a run stops being reproducible, so everything
    # shared is handed out from one place.
    from simulator.clock import SimulatedClock, utc
    from simulator.ports import PortRegistry
    from simulator.rng import RandomSource
    from simulator.world import World

    world = World(pack=pack, clock=SimulatedClock(start=utc(2026, 3, 2)),
                  rng=RandomSource(1), silos={},
                  ports=PortRegistry(path=tmp_path / "ports.json", ports={}))

    first = world.context("a")
    second = world.context("b")
    assert first.now == second.now == utc(2026, 3, 2)
    # Counters are shared by reference, because an id generator needs a
    # counter that outlives a single event and a context does not.
    first.counters["cust"] = 7
    assert second.counters["cust"] == 7


def test_asking_for_a_silo_that_is_not_there_lists_what_is(pack, tmp_path):
    from simulator.clock import SimulatedClock, utc
    from simulator.ports import PortRegistry
    from simulator.rng import RandomSource
    from simulator.world import World

    world = World(pack=pack, clock=SimulatedClock(start=utc(2026, 1, 1)),
                  rng=RandomSource(1), silos={},
                  ports=PortRegistry(path=tmp_path / "p.json", ports={}))
    with pytest.raises(KeyError, match="no silo 'warehouse'"):
        world.silo("warehouse")
    with pytest.raises(KeyError, match="does not hold a database"):
        world.database("payroll")


# -- silo construction -----------------------------------------------

def test_a_file_silo_refuses_a_database(tmp_path):
    # Refusing rather than ignoring: a pack declaring one would have
    # written something with no effect and no way to find out.
    from simulator.silos import build_silo

    silo = build_silo("sqlite", "ledger", tmp_path)
    with pytest.raises(SiloError, match="holds no database"):
        silo.connection("ledger")


def test_the_loader_refuses_a_database_on_a_kind_that_has_none():
    with pytest.raises(PackError, match="holds no databases"):
        load_spec({
            "pack": "x",
            "silos": {"payroll": {"kind": "filedrop", "database": "nope"}},
        })


def test_a_port_requiring_silo_without_a_port_is_refused(tmp_path):
    from simulator.silos import build_silo

    with pytest.raises(SiloError, match="needs a port"):
        build_silo("postgresql", "ops", tmp_path)


def test_a_file_silo_given_a_port_is_refused(tmp_path):
    from simulator.silos import build_silo

    with pytest.raises(SiloError, match="cannot take a port"):
        build_silo("sqlite", "ledger", tmp_path, port=5432)


def test_an_option_a_silo_does_not_take_is_refused(tmp_path):
    # Named here rather than surfacing as a TypeError from a constructor.
    from simulator.silos import build_silo

    with pytest.raises(SiloError, match="does not accept these options"):
        build_silo("sqlite", "ledger", tmp_path, options={"wibble": 1})


# -- teardown --------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.mariadb
def test_stopping_leaves_nothing_reachable(pack, tmp_path, postgres_binaries,
                                           mariadb_binaries):
    built = runner.build(pack, tmp_path / "var", seed=1)
    runner.stop(built)
    for name in ("dispatch", "shop", "books"):
        assert not built.silo(name).is_reachable(), name


@pytest.mark.postgres
@pytest.mark.mariadb
def test_stopping_twice_is_quiet(pack, tmp_path, postgres_binaries, mariadb_binaries):
    built = runner.build(pack, tmp_path / "var", seed=1)
    runner.stop(built)
    runner.stop(built)


def test_a_silo_that_will_not_stop_during_cleanup_is_reported(tmp_path, monkeypatch):
    # A half-built world stops whatever it started, which is right. But
    # a silo that will not STOP during that cleanup is the thing that
    # makes the NEXT run fail on a port conflict, and it used to be
    # swallowed -- so the two failures were never seen together.
    from simulator.silo import SiloError
    from simulator.silos import sqlite as sqlite_silo

    pack = load_spec({
        "pack": "stubborn",
        "silos": {"a": {"kind": "sqlite"}, "b": {"kind": "sqlite"}},
    })

    created = {"count": 0}
    original_create = sqlite_silo.SqliteSilo.create

    def create(self):
        created["count"] += 1
        if created["count"] == 2:
            raise SiloError("the second silo could not be created")
        return original_create(self)

    def refuse_to_stop(self):
        raise SiloError("will not stop")

    monkeypatch.setattr(sqlite_silo.SqliteSilo, "create", create)
    monkeypatch.setattr(sqlite_silo.SqliteSilo, "stop", refuse_to_stop)

    with pytest.raises(SiloError) as raised:
        runner.build(pack, tmp_path / "var")

    message = str(raised.value)
    # BOTH failures, and the original one first: the second is why the
    # next attempt will fail for a reason unrelated to this one.
    assert "could not be created" in message
    assert "would not stop" in message
    assert "will not stop" in message
