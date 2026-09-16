"""Tests for entities: things that persist, hold a state, and move.

A lifecycle is otherwise internal -- entities walk their states in
memory and nothing outside can see it. What is worth pinning is that
the progression reaches the database, that it reaches the RIGHT row,
and that a pack cannot declare a persistence that does not add up.
"""

import textwrap

import pytest
from worlds import running_world, write_pack

from simulator import runner
from simulator.relational import fetch_all
from simulator.spec import PackError, load_spec

JOBS = textwrap.dedent("""
    pack: field_service

    silos:
      dispatch: {kind: postgresql, database: dispatch}

    schemas:
      dispatch:
        tables:
          customers:
            columns:
              customer_id: {type: text, length: 64, primary_key: true, nullable: false}
          work_orders:
            columns:
              work_order_id: {type: text, length: 64, primary_key: true, nullable: false}
              customer_id:   {type: text, length: 64, nullable: false}
              status:        {type: text, length: 32, nullable: false}

    lifecycles:
      WorkOrder:
        initial: requested
        persisted_to: dispatch.work_orders
        state_column: status
        states:
          requested:
            - {to: quoted, per_hour: 0.5}
            - {to: cancelled, per_hour: 0.05}
          quoted:
            - {to: approved, per_hour: 0.2, min_dwell: 2h}
          approved:
            - {to: completed, per_hour: 0.1, min_dwell: 4h}
          completed:
          cancelled:

    seed:
      - table: dispatch.customers
        count: 6
        columns:
          customer_id: {generator: id, prefix: cust}

    events:
      job_raised:
        per: dispatch.customers
        rate_per_hour: 0.15
        emits:
          - table: dispatch.work_orders
            spawns: WorkOrder
            columns:
              work_order_id: {generator: id, prefix: wo}
              customer_id:   {generator: reference, from: subject.customer_id}
              status:        {generator: constant, value: requested}
    """)


@pytest.fixture
def world(tmp_path, postgres_binaries):
    with running_world(tmp_path, JOBS, seed=9) as built:
        yield built


def statuses(world):
    rows = fetch_all(world.silo("dispatch"), "dispatch",
                     "SELECT status, count(*) FROM work_orders GROUP BY status")
    return {status: int(count) for status, count in rows}


# -- entities reach the database -------------------------------------

@pytest.mark.postgres
def test_an_emission_starts_a_tracked_entity(world):
    runner.run(world, total_seconds=43200, tick_seconds=1800)
    assert world.living("WorkOrder"), "no work orders were raised"
    rows = fetch_all(world.silo("dispatch"), "dispatch",
                     "SELECT count(*) FROM work_orders")
    assert int(rows[0][0]) == len(world.living("WorkOrder"))


@pytest.mark.postgres
def test_the_entity_id_is_the_row_it_came_from(world):
    # Not a fresh number with a mapping alongside: a second mapping is a
    # second thing that can fall out of step with the database.
    runner.run(world, total_seconds=43200, tick_seconds=1800)
    in_memory = {entity.entity_id for entity in world.living("WorkOrder")}
    in_database = {row[0] for row in fetch_all(
        world.silo("dispatch"), "dispatch", "SELECT work_order_id FROM work_orders")}
    assert in_memory == in_database


@pytest.mark.postgres
def test_state_changes_are_written_back(world):
    runner.run(world, total_seconds=3 * 86400, tick_seconds=1800)
    census = statuses(world)
    # Everything starts in `requested`, so anything else is progression
    # that reached the database.
    assert set(census) - {"requested"}, f"nothing ever moved: {census}"
    assert census.get("completed", 0) > 0 or census.get("approved", 0) > 0


@pytest.mark.postgres
def test_every_row_agrees_with_the_entity_it_tracks(world):
    # The invariant that matters: the database is what a consumer sees,
    # and it must not lag the simulator's own idea of the world.
    runner.run(world, total_seconds=3 * 86400, tick_seconds=1800)
    in_database = dict(fetch_all(world.silo("dispatch"), "dispatch",
                                 "SELECT work_order_id, status FROM work_orders"))
    in_memory = {entity.entity_id: entity.state for entity in world.living("WorkOrder")}
    assert in_database == in_memory


@pytest.mark.postgres
def test_terminal_states_accumulate_and_working_states_do_not(world):
    # A state machine that never settles would show every state growing
    # together, which is what a missing min_dwell or a wrong rate looks
    # like from outside.
    runner.run(world, total_seconds=5 * 86400, tick_seconds=1800)
    census = statuses(world)
    terminal = census.get("completed", 0) + census.get("cancelled", 0)
    working = sum(census.get(state, 0) for state in ("requested", "quoted", "approved"))
    assert terminal > working


@pytest.mark.postgres
def test_a_lifecycle_without_persistence_stays_internal(tmp_path, postgres_binaries):
    # Legitimate: a pack may use a state machine to drive behaviour
    # without the business system having a column for it.
    source = (JOBS.replace("    persisted_to: dispatch.work_orders\n", "")
                  .replace("    state_column: status\n", ""))
    built = runner.build(write_pack(tmp_path, source, "internal"), tmp_path / "internal", seed=9)
    try:
        runner.seed(built)
        runner.run(built, total_seconds=3 * 86400, tick_seconds=1800)
        # Entities moved...
        assert {e.state for e in built.living("WorkOrder")} != {"requested"}
        # ...and the column never did.
        assert set(statuses(built)) == {"requested"}
    finally:
        runner.stop(built)


# -- declaration, checked at load ------------------------------------

def base(lifecycle=None, emission=None) -> dict:
    return {
        "pack": "x",
        "silos": {"d": {"kind": "postgresql", "database": "d"}},
        "schemas": {"d": {"tables": {
            "work_orders": {"columns": {
                "work_order_id": {"type": "text", "length": 64, "primary_key": True,
                                  "nullable": False},
                "status": {"type": "text", "length": 32, "nullable": False},
                "total": {"type": "decimal", "precision": 19, "scale": 4}}},
            "notes": {"columns": {
                "note": {"type": "text", "length": 64, "nullable": False}}},
        }}},
        "lifecycles": {"WorkOrder": lifecycle or {
            "initial": "a", "persisted_to": "d.work_orders",
            "state_column": "status", "states": {"a": None}}},
        "events": {"raise": {"rate_per_hour": 1.0, "emits": [emission or {
            "table": "d.work_orders", "spawns": "WorkOrder",
            "columns": {"work_order_id": {"generator": "id", "prefix": "wo"},
                        "status": {"generator": "constant", "value": "a"}}}]}},
    }


def test_spawning_an_unknown_lifecycle_is_refused():
    with pytest.raises(PackError, match="no lifecycle called 'Nope'"):
        load_spec(base(emission={
            "table": "d.work_orders", "spawns": "Nope",
            "columns": {"work_order_id": {"generator": "id", "prefix": "wo"},
                        "status": {"generator": "constant", "value": "a"}}}))


def test_a_lifecycle_with_nowhere_to_write_can_still_be_spawned():
    # Briefly refused, which made a non-persisted lifecycle declarable
    # and impossible to instantiate -- there was no other way to create
    # an entity. Driving behaviour without a column for it is a
    # legitimate thing for a pack to want.
    pack = load_spec(base(lifecycle={"initial": "a", "states": {"a": None}}))
    assert "WorkOrder" not in pack.persistence
    assert pack.events[0].emissions[0].spawns == "WorkOrder"


def test_spawning_into_the_wrong_table_is_refused():
    # The entity's id is the row's primary key, so the row has to be in
    # the table the lifecycle lives in.
    with pytest.raises(PackError, match="cannot be started by an emission into"):
        load_spec(base(emission={
            "table": "d.notes", "spawns": "WorkOrder",
            "columns": {"note": {"generator": "constant", "value": "x"}}}))


def test_spawning_without_generating_the_key_is_refused():
    # Caught by the non-null check rather than a spawn-specific one: the
    # key is the primary key, which cannot be nullable, so a generator
    # for it is already required. A second check would be unreachable,
    # and one was written and removed on exactly that ground.
    with pytest.raises(PackError, match=r"non-null column\(s\) \['work_order_id'\]"):
        load_spec(base(emission={
            "table": "d.work_orders", "spawns": "WorkOrder",
            "columns": {"status": {"generator": "constant", "value": "a"}}}))


def test_spawning_into_a_table_with_no_primary_key_is_refused():
    with pytest.raises(PackError, match="needs .* to have a primary key"):
        load_spec(base(
            lifecycle={"initial": "a", "states": {"a": None}},
            emission={"table": "d.notes", "spawns": "WorkOrder",
                      "columns": {"note": {"generator": "constant", "value": "x"}}}))


def test_a_non_text_state_column_is_refused():
    # A state column holds names.
    with pytest.raises(PackError, match="must be text"):
        load_spec(base(lifecycle={
            "initial": "a", "persisted_to": "d.work_orders",
            "state_column": "total", "states": {"a": None}}))


def test_a_state_column_that_does_not_exist_is_refused():
    with pytest.raises(PackError, match="has no column 'stage'"):
        load_spec(base(lifecycle={
            "initial": "a", "persisted_to": "d.work_orders",
            "state_column": "stage", "states": {"a": None}}))


def test_persisting_to_a_table_with_no_primary_key_is_refused():
    # Without one there is no way to find the row again when the entity
    # moves.
    with pytest.raises(PackError, match="has no primary key"):
        load_spec(base(lifecycle={
            "initial": "a", "persisted_to": "d.notes",
            "state_column": "note", "states": {"a": None}}))


def test_a_state_column_without_a_table_is_refused():
    with pytest.raises(PackError, match="no persisted_to table"):
        load_spec(base(lifecycle={
            "initial": "a", "state_column": "status", "states": {"a": None}}))
