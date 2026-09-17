"""Tests for picking a world up where it was left.

Every run before this produced days, because a world existed only
while the process that built it was alive. A consumer asking how
something changed last quarter had nothing to work with, and a year
cannot be produced in one sitting if stopping loses it.
"""

import textwrap

import pytest
from worlds import write_pack

from simulator import runner
from simulator.relational import fetch_all
from simulator.resume import STATE_FILENAME, ResumeError, resume, save

SHOP = textwrap.dedent("""
    pack: resumable

    silos:
      ops: {kind: postgresql, database: ops}

    schemas:
      ops:
        tables:
          tills:
            columns:
              till_id: {type: text, length: 64, primary_key: true, nullable: false}
          orders:
            columns:
              order_id: {type: text, length: 64, primary_key: true, nullable: false}
              status:   {type: text, length: 32, nullable: false}
              total:    {type: decimal, precision: 19, scale: 4, nullable: false}

    lifecycles:
      Order:
        initial: open
        persisted_to: ops.orders
        state_column: status
        states:
          open:
            - {to: settled, per_hour: 0.5}
          settled:

    seed:
      - table: ops.tills
        count: 2
        columns:
          till_id: {generator: id, prefix: till}

    events:
      order_placed:
        per: ops.tills
        rate_per_hour: 1.0
        emits:
          - table: ops.orders
            spawns: Order
            columns:
              order_id: {generator: id, prefix: ord}
              status:   {generator: constant, value: open}
              total:    {generator: decimal, min: 5, max: 90, scale: 4}
    """)


def first_leg(tmp_path, days=2):
    """Build a world, run it, stop it the way the command line does."""
    directory = tmp_path / "var"
    world = runner.build(write_pack(tmp_path, SHOP, "resumable"), directory, seed=4)
    runner.seed(world)
    runner.run(world, total_seconds=days * 86400, tick_seconds=3600)
    facts = {
        "clock": world.clock.now(),
        "orders": fetch_all(world.silo("ops"), "ops",
                            "SELECT count(*) FROM orders")[0][0],
        "entities": len(world.entities.get("Order", [])),
        "counter": world.counters.get("ord", 0),
    }
    save(world, directory)
    runner.stop(world)
    return directory, facts


def second_leg(tmp_path, directory):
    world = runner.attach(write_pack(tmp_path, SHOP, "resumable"), directory, seed=4)
    restored = resume(world, directory)
    return world, restored


# -- what comes back ---------------------------------------------------

@pytest.mark.postgres
def test_a_world_can_be_picked_up_where_it_stopped(tmp_path, postgres_binaries):
    directory, before = first_leg(tmp_path)
    world, restored = second_leg(tmp_path, directory)
    try:
        assert world.clock.now() == before["clock"]
        assert restored["Order"] == before["entities"] > 0
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_entities_come_from_the_database_not_a_file(tmp_path, postgres_binaries):
    # A lifecycle's state is written to a real column, so the database
    # already holds it and a second copy would be a second thing to
    # disagree. Deleting the state file leaves only the clock missing.
    directory, before = first_leg(tmp_path)
    (directory / STATE_FILENAME).unlink()

    world = runner.attach(write_pack(tmp_path, SHOP, "resumable"), directory, seed=4)
    try:
        with pytest.raises(ResumeError, match="never stopped cleanly"):
            resume(world, directory)
        # ...and the entities are still there to be read.
        from simulator.resume import restore_entities
        assert restore_entities(world)["Order"] == before["entities"]
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_counters_are_derived_from_the_rows(tmp_path, postgres_binaries):
    # A counter kept in a file could disagree with the data after
    # anything else wrote to it -- and this simulator hands out a
    # writer account precisely so that something else can.
    from simulator.resume import restore_counters

    directory, before = first_leg(tmp_path)
    world = runner.attach(write_pack(tmp_path, SHOP, "resumable"), directory, seed=4)
    try:
        counters = restore_counters(world)
        assert counters["ord"] == before["counter"] > 0
        assert counters["till"] == 2
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_a_second_leg_continues_rather_than_colliding(tmp_path, postgres_binaries):
    # THE thing that has to work. Ids carry on from where they were, so
    # the second leg's writes land beside the first leg's rather than
    # on top of them.
    directory, before = first_leg(tmp_path)
    world, _ = second_leg(tmp_path, directory)
    try:
        runner.run(world, total_seconds=2 * 86400, tick_seconds=3600)
        total, distinct = fetch_all(
            world.silo("ops"), "ops",
            "SELECT count(*), count(DISTINCT order_id) FROM orders")[0]
        assert total == distinct, "the second leg reissued ids the first had used"
        assert total > before["orders"]
        assert world.clock.now() > before["clock"]
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_a_resumed_world_is_not_seeded_again(tmp_path, postgres_binaries):
    # Seeding again would collide on every reference key it wrote the
    # first time.
    directory, _ = first_leg(tmp_path)
    world, _ = second_leg(tmp_path, directory)
    try:
        tills = fetch_all(world.silo("ops"), "ops", "SELECT count(*) FROM tills")[0][0]
        assert tills == 2
    finally:
        runner.stop(world)


# -- what it refuses to do quietly -------------------------------------

@pytest.mark.postgres
def test_a_resume_with_no_entities_refuses(tmp_path, postgres_binaries):
    # This failure is indistinguishable from a working resume until the
    # first event fires. An earlier prototype hit exactly it and
    # produced 3,360 sales with not one attributable to a customer.
    directory, _ = first_leg(tmp_path)
    world = runner.attach(write_pack(tmp_path, SHOP, "resumable"), directory, seed=4)
    try:
        with world.silo("ops").connect("ops", autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE orders SET status = 'archived'")
        with pytest.raises(ResumeError, match="no live entities"):
            resume(world, directory)
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_a_row_whose_state_is_not_a_lifecycle_state_is_left_alone(tmp_path,
                                                                  postgres_binaries):
    # Real data an old system left behind is not a live entity, and
    # loading it as one would give the simulation something to move
    # that the lifecycle has no rules for.
    directory, before = first_leg(tmp_path)
    world = runner.attach(write_pack(tmp_path, SHOP, "resumable"), directory, seed=4)
    try:
        with world.silo("ops").connect("ops", autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO orders VALUES ('ord_legacy', 'archived', '1.0000')")
        restored = resume(world, directory)
        assert restored["Order"] == before["entities"]
    finally:
        runner.stop(world)


def test_nothing_to_resume_says_so(tmp_path):
    from simulator.resume import existing_world

    assert existing_world(tmp_path) is False
