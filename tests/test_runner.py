"""Tests for building a world from a pack and seeding it.

The end-to-end cases run against real engines, because this layer's
whole job is to make a YAML file become running databases with rows in
them, and a mock would only confirm that the code calls the methods the
mock expects.
"""

import textwrap

import pytest

from simulator import runner
from simulator.event import EventError
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


# -- seeding more than fits in memory ---------------------------------

@pytest.mark.postgres
def test_a_seed_larger_than_a_chunk_writes_every_row(tmp_path, postgres_binaries):
    # Measured before chunking: two million rows cost 1.1 GB and grew
    # linearly, so a pack an order of magnitude larger simply would not
    # fit. After: 58 MB, flat.
    source = textwrap.dedent("""
        pack: big
        silos: {ops: {kind: postgresql, database: ops}}
        schemas:
          ops:
            tables:
              rows:
                columns:
                  row_id: {type: text, length: 64, primary_key: true, nullable: false}
                  label:  {type: text, length: 64, nullable: false}
        seed:
          - table: ops.rows
            count: 12000
            columns:
              row_id: {generator: id, prefix: r}
              label:  {generator: template, pattern: "row {row_id}"}
        """)
    path = tmp_path / "big.yaml"
    path.write_text(source)
    world = runner.build(load_pack(path), tmp_path / "var")
    try:
        assert runner.seed(world) == {"ops.rows": 12000}
        assert count_rows(world.silo("ops"), "ops", "rows") == 12000
        # And the ids are still a single unbroken sequence across
        # chunks, which a per-chunk counter would have restarted.
        first, last = fetch_all(world.silo("ops"), "ops",
                                "SELECT min(row_id), max(row_id) FROM rows")[0]
        assert first == "r_000001"
        assert last == "r_012000"
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_seeding_really_writes_in_chunks(tmp_path, monkeypatch, postgres_binaries):
    # The end state -- every row present -- is identical whether the
    # rows went in as one statement or many, so it cannot tell chunking
    # from not chunking. Count the writes instead.
    source = textwrap.dedent("""
        pack: chunked
        silos: {ops: {kind: postgresql, database: ops}}
        schemas:
          ops:
            tables:
              rows:
                columns:
                  row_id: {type: text, length: 64, primary_key: true, nullable: false}
        seed:
          - table: ops.rows
            count: 12000
            columns:
              row_id: {generator: id, prefix: r}
        """)
    path = tmp_path / "chunked.yaml"
    path.write_text(source)
    world = runner.build(load_pack(path), tmp_path / "var")

    sizes = []
    original = runner.insert_rows

    def observe(silo, database, table, rows):
        sizes.append(len(rows))
        return original(silo, database, table, rows)

    monkeypatch.setattr(runner, "insert_rows", observe)
    try:
        runner.seed(world)
    finally:
        runner.stop(world)

    assert sum(sizes) == 12000
    assert len(sizes) == 3, sizes
    assert max(sizes) <= runner.SEED_CHUNK_ROWS


@pytest.mark.postgres
def test_a_failure_part_way_through_seeding_leaves_nothing(tmp_path,
                                                            postgres_binaries):
    # One connection per silo across every step, so seeding is atomic:
    # a world half-seeded is not a state any business is ever in.
    source = textwrap.dedent("""
        pack: halting
        silos: {ops: {kind: postgresql, database: ops}}
        schemas:
          ops:
            tables:
              rows:
                columns:
                  row_id: {type: text, length: 64, primary_key: true, nullable: false}
        seed:
          - table: ops.rows
            count: 6000
            columns:
              row_id: {generator: id, prefix: r}
          - table: ops.rows
            count: 3
            columns:
              row_id: {generator: constant, value: clash}
        """)
    path = tmp_path / "halting.yaml"
    path.write_text(source)
    world = runner.build(load_pack(path), tmp_path / "var")
    try:
        import psycopg

        with pytest.raises(psycopg.IntegrityError):
            runner.seed(world)
        # The first step's six thousand rows went in before the second
        # step collided on its own duplicate key.
        assert count_rows(world.silo("ops"), "ops", "rows") == 0
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_seeding_leaves_no_stale_view_of_a_table_it_wrote(tmp_path, postgres_binaries):
    # A seed step that PICKS from a table fills the subject cache with
    # the rows existing at that moment, and a later step adding to that
    # table leaves the cache behind. Every event afterwards is `per` the
    # stale list.
    #
    # Found for real: three duplicate customers, seeded after a step
    # that picked from customers, never raised a single job and looked
    # like records nobody had ever called about.
    source = textwrap.dedent("""
        pack: stale
        silos: {ops: {kind: postgresql, database: ops}}
        schemas:
          ops:
            tables:
              people:
                columns:
                  person_id: {type: text, length: 64, primary_key: true, nullable: false}
                  name:      {type: text, length: 64, nullable: false}
              calls:
                columns:
                  call_id:   {type: text, length: 64, primary_key: true, nullable: false}
                  person_id: {type: text, length: 64, nullable: false}
        seed:
          - table: ops.people
            count: 3
            columns:
              person_id: {generator: id, prefix: p}
              name:      {generator: template, pattern: "Person {person_id}"}
          - table: ops.people
            count: 2
            picks: [ops.people]
            columns:
              person_id: {generator: id, prefix: p}
              name:      {generator: reference, from: picked.people.name}
        events:
          called:
            per: ops.people
            rate_per_hour: 4.0
            emits:
              - table: ops.calls
                columns:
                  call_id:   {generator: id, prefix: c}
                  person_id: {generator: reference, from: subject.person_id}
        """)
    path = tmp_path / "stale.yaml"
    path.write_text(source)
    world = runner.build(load_pack(path), tmp_path / "var", seed=3)
    try:
        runner.seed(world)
        runner.run(world, total_seconds=2 * 86400, tick_seconds=3600)
        called = fetch_all(world.silo("ops"), "ops",
                           "SELECT count(DISTINCT person_id) FROM calls")[0][0]
        assert called == 5, "the later-seeded people were never called"
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_distinct_picks_refuse_to_repeat_within_a_subject(tmp_path, postgres_binaries):
    # And they run out honestly: asking for more distinct picks than
    # there are rows is an error naming both numbers, not a silent
    # repeat.
    source = textwrap.dedent("""
        pack: tickets
        silos: {ops: {kind: postgresql, database: ops}}
        schemas:
          ops:
            tables:
              people:
                columns:
                  person_id: {type: text, length: 64, primary_key: true, nullable: false}
              tickets:
                columns:
                  ticket_id: {type: text, length: 64, primary_key: true, nullable: false}
              held:
                columns:
                  held_id:   {type: text, length: 64, primary_key: true, nullable: false}
                  person_id: {type: text, length: 64, nullable: false}
                  ticket_id: {type: text, length: 64, nullable: false}
        seed:
          - table: ops.people
            count: 6
            columns: {person_id: {generator: id, prefix: p}}
          - table: ops.tickets
            count: 3
            columns: {ticket_id: {generator: id, prefix: t}}
          - table: ops.held
            per: ops.people
            count: 3
            picks: [ops.tickets]
            distinct_picks: true
            columns:
              held_id:   {generator: id, prefix: h}
              person_id: {generator: reference, from: subject.person_id}
              ticket_id: {generator: reference, from: picked.tickets.ticket_id}
        """)
    path = tmp_path / "tickets.yaml"
    path.write_text(source)
    world = runner.build(load_pack(path), tmp_path / "var", seed=5)
    try:
        runner.seed(world)
        rows, distinct = fetch_all(
            world.silo("ops"), "ops",
            "SELECT count(*), count(DISTINCT (person_id, ticket_id)) FROM held")[0]
        assert rows == 18, rows
        assert rows == distinct, "somebody was given the same ticket twice"
        # Every ticket, for everybody: three distinct from three is the
        # whole set, which is the tightest case that can still succeed.
        each = fetch_all(world.silo("ops"), "ops",
                         "SELECT count(DISTINCT ticket_id) FROM held "
                         "GROUP BY person_id")
        assert {count for (count,) in each} == {3}
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_asking_for_more_distinct_picks_than_exist_says_so(tmp_path, postgres_binaries):
    source = textwrap.dedent("""
        pack: toomany
        silos: {ops: {kind: postgresql, database: ops}}
        schemas:
          ops:
            tables:
              people:
                columns:
                  person_id: {type: text, length: 64, primary_key: true, nullable: false}
              tickets:
                columns:
                  ticket_id: {type: text, length: 64, primary_key: true, nullable: false}
              held:
                columns:
                  held_id:   {type: text, length: 64, primary_key: true, nullable: false}
                  ticket_id: {type: text, length: 64, nullable: false}
        seed:
          - table: ops.people
            count: 2
            columns: {person_id: {generator: id, prefix: p}}
          - table: ops.tickets
            count: 2
            columns: {ticket_id: {generator: id, prefix: t}}
          - table: ops.held
            per: ops.people
            count: 5
            picks: [ops.tickets]
            distinct_picks: true
            columns:
              held_id:   {generator: id, prefix: h}
              ticket_id: {generator: reference, from: picked.tickets.ticket_id}
        """)
    path = tmp_path / "toomany.yaml"
    path.write_text(source)
    world = runner.build(load_pack(path), tmp_path / "var", seed=5)
    try:
        with pytest.raises(SiloError, match="5 distinct picks.*only 2 rows"):
            runner.seed(world)
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_an_updates_pick_sees_rows_written_during_the_run(tmp_path, postgres_binaries):
    # THE property, and the retail pack cannot test it: its stocktake
    # picks from products, which are seeded once, so a cached read and
    # a fresh one return the same rows and the control aimed at
    # freshness stayed silent.
    #
    # Here the picked table GROWS. A cached pick would choose only from
    # rows that existed at startup -- and at startup there are none, so
    # a cache would make this fail outright rather than subtly, which
    # is the one mercy in it.
    source = textwrap.dedent("""
        pack: fresh
        silos: {ops: {kind: postgresql, database: ops}}
        schemas:
          ops:
            tables:
              bills:
                columns:
                  bill_id: {type: text, length: 64, primary_key: true, nullable: false}
                  voided:  {type: boolean, nullable: false}
        events:
          billed:
            every: 1h
            emits:
              - table: ops.bills
                columns:
                  bill_id: {generator: id, prefix: b}
                  voided:  {generator: constant, value: false}
          disputed:
            every: 6h
            emits:
              - update: ops.bills
                picks: [ops.bills]
                where: {bill_id: {generator: reference, from: picked.bills.bill_id}}
                columns: {voided: {generator: constant, value: true}}
        """)
    path = tmp_path / "fresh.yaml"
    path.write_text(source)
    world = runner.build(load_pack(path), tmp_path / "var", seed=3)
    try:
        runner.seed(world)
        runner.run(world, total_seconds=10 * 86400, tick_seconds=3600)
        total, voided = fetch_all(
            world.silo("ops"), "ops",
            "SELECT count(*), count(*) FILTER (WHERE voided) FROM bills")[0]
        assert total > 100
        assert 0 < voided < total, (voided, total)

        # And the ones voided are spread across the run rather than
        # confined to whatever existed at the start -- which is nothing.
        highest = fetch_all(world.silo("ops"), "ops",
                            "SELECT max(bill_id) FROM bills WHERE voided")[0][0]
        first_few = sorted(row[0] for row in fetch_all(
            world.silo("ops"), "ops", "SELECT bill_id FROM bills ORDER BY bill_id"))[:20]
        assert highest not in first_few, (
            "only early rows were ever picked, which is what a stale cache does")
    finally:
        runner.stop(world)



ATOMIC = textwrap.dedent("""
    pack: atomic
    silos: {ops: {kind: postgresql, database: ops}}
    schemas:
      ops:
        tables:
          jobs:
            columns:
              job_id: {type: text, length: 64, primary_key: true, nullable: false}
              status: {type: text, length: 32, nullable: false}
    lifecycles:
      Job:
        initial: open
        persisted_to: ops.jobs
        state_column: status
        states:
          open:
            - {to: done, per_hour: 4.0}
          done:
    events:
      raised:
        every: 1h
        emits:
          - table: ops.jobs
            spawns: Job
            columns:
              job_id: {generator: id, prefix: j}
              status: {generator: constant, value: open}
    """)


def atomic_world(tmp_path, name="var"):
    path = tmp_path / "atomic.yaml"
    path.write_text(ATOMIC)
    world = runner.build(load_pack(path), tmp_path / name, seed=2)
    runner.seed(world)
    return world


def break_emitting(monkeypatch):
    """Make every insert die AFTER it has written, mid-tick."""
    from simulator import event as event_module

    original = event_module.InsertEmission.emit

    def explode(self, world_, context):
        original(self, world_, context)
        raise RuntimeError("the tick fell over")

    monkeypatch.setattr(event_module.InsertEmission, "emit", explode)
    return lambda: monkeypatch.setattr(event_module.InsertEmission, "emit", original)


@pytest.mark.postgres
def test_a_failed_tick_leaves_memory_where_the_databases_are(tmp_path, monkeypatch,
                                                             postgres_binaries):
    # A silo's session rolls its writes back on an exception, and
    # nothing rolled back the entities that had moved state or the id
    # counters that had been handed out. So a failed tick left the
    # world believing things its databases had never been told, and the
    # next tick wrote rows numbered from a counter the database knew
    # nothing about.
    world = atomic_world(tmp_path)
    try:
        runner.run(world, total_seconds=6 * 3600, tick_seconds=3600)
        rows = fetch_all(world.silo("ops"), "ops", "SELECT count(*) FROM jobs")[0][0]
        states = {entity.entity_id: entity.state for entity in world.entities["Job"]}
        counters = dict(world.counters)
        elapsed = world.clock.elapsed

        break_emitting(monkeypatch)
        # Wrapped by the runner, which names the event and the table --
        # the RuntimeError is what the emission raised underneath.
        with pytest.raises(EventError, match="the tick fell over"):
            runner.tick(world, 3600)

        assert fetch_all(world.silo("ops"), "ops",
                         "SELECT count(*) FROM jobs")[0][0] == rows
        assert {e.entity_id: e.state for e in world.entities["Job"]} == states, (
            "entities moved on without the database")
        assert dict(world.counters) == counters, (
            "an id was handed out that nothing was written under")
        assert world.clock.elapsed == elapsed, "the clock ran on"
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_the_world_carries_on_after_a_failed_tick(tmp_path, monkeypatch,
                                                  postgres_binaries):
    # Putting memory back is only useful if the next tick works.
    world = atomic_world(tmp_path)
    try:
        runner.run(world, total_seconds=3 * 3600, tick_seconds=3600)
        restore = break_emitting(monkeypatch)
        with pytest.raises(EventError):
            runner.tick(world, 3600)
        restore()

        runner.tick(world, 3600)
        total, distinct = fetch_all(
            world.silo("ops"), "ops",
            "SELECT count(*), count(DISTINCT job_id) FROM jobs")[0]
        assert total == distinct, "an id was reused after the failed tick"
        assert total > 3
    finally:
        runner.stop(world)
