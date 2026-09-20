"""A reporting copy that is always slightly behind.

A reporting tool is usually pointed at one of these rather than at the
system of record, so "the number was right five minutes ago" is a real
support call -- and it was the last common deployment shape this
simulator could not produce.
"""

import textwrap

import pytest
from worlds import write_pack

from simulator import runner
from simulator.relational import fetch_all
from simulator.spec import load_spec
from simulator.spec.values import PackError

LAGGING = textwrap.dedent("""
    pack: lagging
    silos:
      live:      {kind: postgresql, database: live}
      reporting: {kind: postgresql, database: reporting,
                  replicates: live, refresh: 6h}
    schemas:
      live:
        tables:
          orders:
            columns:
              order_id: {type: text, length: 64, primary_key: true, nullable: false}
              total:    {type: decimal, precision: 19, scale: 4, nullable: false}
    seed:
      - table: live.orders
        count: 5
        columns:
          order_id: {generator: id, prefix: o}
          total:    {generator: constant, value: "10.0000"}
    events:
      ordered:
        every: 1h
        emits:
          - table: live.orders
            columns:
              order_id: {generator: id, prefix: o}
              total:    {generator: constant, value: "10.0000"}
    """)


def counts(world):
    return (fetch_all(world.silo("live"), "live",
                      "SELECT count(*) FROM orders")[0][0],
            fetch_all(world.silo("reporting"), "reporting",
                      "SELECT count(*) FROM orders")[0][0])


@pytest.mark.postgres
def test_a_replica_falls_behind_and_catches_up(tmp_path, postgres_binaries):
    # The sawtooth a refreshed copy really has: it drifts further
    # behind every tick, then comes level when the interval comes
    # round. Between refreshes it is behind by up to that interval,
    # and that IS the lag -- a materialised copy is exactly as stale as
    # the time since it was last rebuilt.
    world = runner.build(write_pack(tmp_path, LAGGING, "lagging"),
                         tmp_path / "var", seed=1)
    try:
        runner.seed(world)
        gaps = []
        for _ in range(15):
            runner.run(world, total_seconds=3600, tick_seconds=3600)
            live, replica = counts(world)
            gaps.append(live - replica)

        assert max(gaps) > 0, "the replica never fell behind"
        assert gaps.count(0) >= 2, "it never caught up"
        assert all(gap >= 0 for gap in gaps), "the copy was ahead of its source"
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_a_replica_is_a_real_database_a_consumer_can_read(tmp_path,
                                                          postgres_binaries):
    # It gets its own database, its own tables and its own grants --
    # it is not a view or a trick, which is what makes a consumer
    # pointed at it behave exactly as one pointed at a real replica.
    world = runner.build(write_pack(tmp_path, LAGGING, "lagging"),
                         tmp_path / "var", seed=1)
    try:
        runner.seed(world)
        runner.run(world, total_seconds=7 * 3600, tick_seconds=3600)

        details = world.connections()["reporting"].details
        assert details["database"] == "reporting"

        import psycopg

        with psycopg.connect(host=details["host"], port=details["port"],
                             dbname=details["database"], user=details["user"],
                             password=details["password"]) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM orders")
                assert cursor.fetchone()[0] > 0
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_a_replica_reflects_changes_and_not_just_additions(tmp_path,
                                                           postgres_binaries):
    # A FULL REBUILD, which is what a materialised copy is. One that
    # copied only recent rows would diverge permanently on anything
    # that changed, and never say so.
    world = runner.build(write_pack(tmp_path, LAGGING, "lagging"),
                         tmp_path / "var", seed=1)
    try:
        runner.seed(world)
        runner.run(world, total_seconds=7 * 3600, tick_seconds=3600)

        with world.silo("live").connect("live", autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE orders SET total = 99 WHERE order_id = 'o_000001'")

        runner.run(world, total_seconds=7 * 3600, tick_seconds=3600)
        copied = fetch_all(world.silo("reporting"), "reporting",
                           "SELECT total FROM orders WHERE order_id = 'o_000001'")[0][0]
        assert int(copied) == 99, "the copy kept a value its source had changed"
    finally:
        runner.stop(world)


# -- what a pack may not say -------------------------------------------

def test_a_replica_needs_a_refresh_interval():
    # Without one the copy would be rebuilt every tick, which is a
    # replica with no lag -- an expensive way of having a second copy
    # of the same answers.
    with pytest.raises(PackError, match="needs a `refresh` interval"):
        load_spec({
            "pack": "x",
            "silos": {"live": {"kind": "postgresql", "database": "live"},
                      "reporting": {"kind": "postgresql", "database": "reporting",
                                    "replicates": "live"}},
            "schemas": {"live": {"tables": {"orders": {"columns": {
                "order_id": {"type": "text", "length": 64, "primary_key": True,
                             "nullable": False}}}}}},
        })


def test_a_refresh_without_a_replica_means_nothing():
    with pytest.raises(PackError, match="only means something with"):
        load_spec({
            "pack": "x",
            "silos": {"live": {"kind": "postgresql", "database": "live",
                               "refresh": "6h"}},
            "schemas": {"live": {"tables": {"orders": {"columns": {
                "order_id": {"type": "text", "length": 64, "primary_key": True,
                             "nullable": False}}}}}},
        })


def test_a_replica_declares_no_schema_of_its_own():
    # Its shape is whatever it copies. Letting a pack declare one too
    # would be letting it declare a copy that differs from its source,
    # which is not a replica but a second database with a confusing
    # name.
    with pytest.raises(PackError, match="remove its schema"):
        load_spec({
            "pack": "x",
            "silos": {"live": {"kind": "postgresql", "database": "live"},
                      "reporting": {"kind": "postgresql", "database": "reporting",
                                    "replicates": "live", "refresh": "6h"}},
            "schemas": {
                "live": {"tables": {"orders": {"columns": {
                    "order_id": {"type": "text", "length": 64, "primary_key": True,
                                 "nullable": False}}}}},
                "reporting": {"tables": {"orders": {"columns": {
                    "order_id": {"type": "text", "length": 64, "primary_key": True,
                                 "nullable": False}}}}},
            },
        })


def test_a_replica_names_a_silo_that_exists():
    with pytest.raises(PackError, match="does not declare"):
        load_spec({
            "pack": "x",
            "silos": {"reporting": {"kind": "postgresql", "database": "reporting",
                                    "replicates": "nowhere", "refresh": "6h"}},
            "schemas": {},
        })


def test_a_replica_of_a_replica_is_refused():
    # A chain's lag is the sum of its links, and nothing here tracks
    # that.
    with pytest.raises(PackError, match="itself a replica"):
        load_spec({
            "pack": "x",
            "silos": {
                "live": {"kind": "postgresql", "database": "live"},
                "first": {"kind": "postgresql", "database": "first",
                          "replicates": "live", "refresh": "6h"},
                "second": {"kind": "postgresql", "database": "second",
                           "replicates": "first", "refresh": "6h"},
            },
            "schemas": {"live": {"tables": {"orders": {"columns": {
                "order_id": {"type": "text", "length": 64, "primary_key": True,
                             "nullable": False}}}}}},
        })


def test_a_silo_cannot_replicate_itself():
    with pytest.raises(PackError, match="replica of itself"):
        load_spec({
            "pack": "x",
            "silos": {"live": {"kind": "postgresql", "database": "live",
                               "replicates": "live", "refresh": "6h"}},
            "schemas": {"live": {"tables": {"orders": {"columns": {
                "order_id": {"type": "text", "length": 64, "primary_key": True,
                             "nullable": False}}}}}},
        })
