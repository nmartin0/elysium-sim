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
              status:       {type: text, length: 32, nullable: false}
              status_since: {type: timestamp}
              total:        {type: decimal, precision: 19, scale: 4, nullable: false}

    lifecycles:
      Order:
        initial: open
        persisted_to: ops.orders
        state_column: status
        entered_column: status_since
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
                    "INSERT INTO orders (order_id, status, total) "
                    "VALUES ('ord_legacy', 'archived', '1.0000')")
        restored = resume(world, directory)
        assert restored["Order"] == before["entities"]
    finally:
        runner.stop(world)


def test_nothing_to_resume_says_so(tmp_path):
    from simulator.resume import existing_world

    assert existing_world(tmp_path) is False


# -- how long an entity has been where it is ---------------------------

@pytest.mark.postgres
def test_a_resumed_entity_keeps_the_dwell_it_had(tmp_path, postgres_binaries):
    # Without this a resumed world knows what state every entity is in
    # and NOT how long it has been there, so any transition gated on
    # dwell waits its full time again -- and a year built from twelve
    # legs restarts every entity's clock twelve times.
    directory, before = first_leg(tmp_path, days=3)
    world, _ = second_leg(tmp_path, directory)
    try:
        entered = {entity.entity_id: entity.entered_state_at
                   for entity in world.entities["Order"]}
        assert entered

        # Every one of them arrived BEFORE the clock this leg starts
        # at: if the column were ignored they would all read as having
        # arrived exactly now.
        now = world.clock.now()
        # NOT "all before now": an entity spawned in the final tick
        # genuinely arrived at the clock, and a first version of this
        # test called that a lost dwell. What a lost dwell really looks
        # like is EVERY arrival being the clock.
        assert max(entered.values()) <= now, (max(entered.values()), now)
        assert min(entered.values()) < now, "the dwell was lost"
        assert len(set(entered.values())) > 1, (
            "every entity arrived at the same instant, which is the clock")

        # And the spread is real: these arrived across the first leg
        # rather than clustering at its end.
        assert (now - min(entered.values())).total_seconds() > 3600
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_the_moment_is_written_whenever_a_state_changes(tmp_path, postgres_binaries):
    # The column is only useful if the simulation keeps it current, and
    # it is the simulated moment rather than the wall clock -- a fact
    # about the business's timeline.
    directory, _ = first_leg(tmp_path, days=3)
    world, _ = second_leg(tmp_path, directory)
    try:
        rows = fetch_all(world.silo("ops"), "ops",
                         "SELECT status, status_since FROM orders")
        assert rows
        assert all(moment is not None for _, moment in rows)
        # Simulated, so within the run rather than around today.
        assert all(moment.year == 2026 for _, moment in rows), rows[:3]
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_a_pack_without_the_column_still_resumes(tmp_path, postgres_binaries):
    # Optional, because most tables do not have such a column. A pack
    # that omits it gets what every world got before this existed:
    # every entity looks freshly arrived.
    from simulator.resume import restore_entities

    source = "\n".join(line for line in SHOP.splitlines()
                       if "entered_column" not in line)
    assert "entered_column" not in source
    directory = tmp_path / "var"
    world = runner.build(write_pack(tmp_path, source, "resumable"), directory, seed=4)
    runner.seed(world)
    runner.run(world, total_seconds=2 * 86400, tick_seconds=3600)
    save(world, directory)
    runner.stop(world)

    world = runner.attach(write_pack(tmp_path, source, "resumable"), directory, seed=4)
    try:
        assert restore_entities(world)["Order"] > 0
        arrivals = {entity.entered_state_at for entity in world.entities["Order"]}
        assert len(arrivals) == 1, "without the column they should all read as now"
    finally:
        runner.stop(world)
