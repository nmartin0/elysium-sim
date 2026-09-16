"""Tests for events fired by a transition, and emissions that revise.

Neither is useful alone -- a transition with nothing to write changes
no data, and an update with no occasion to run never fires -- so they
are tested together against the case that motivated both: the OOOI
milestone model, where one flight leg is created when its schedule is
published and then REVISED four times as it passes Gate Out, Wheels
Off, Wheels On and Gate In. Modelling that as four rows would be a
different thing wearing its name.
"""

import textwrap

import pytest
from worlds import running_world, write_pack

from simulator import runner
from simulator.relational import fetch_all
from simulator.spec import PackError, load_spec

OOOI = textwrap.dedent("""
    pack: aviation

    silos:
      aodb: {kind: postgresql, database: aodb}

    schemas:
      aodb:
        tables:
          aircraft:
            columns:
              registration: {type: text, length: 16, primary_key: true, nullable: false}
          flight_legs:
            columns:
              leg_id:        {type: text, length: 64, primary_key: true, nullable: false}
              registration:  {type: text, length: 16, nullable: false}
              leg_status:    {type: text, length: 24, nullable: false}
              scheduled_out: {type: timestamp, nullable: false}
              actual_out:    {type: timestamp}
              wheels_off:    {type: timestamp}
              wheels_on:     {type: timestamp}
              actual_in:     {type: timestamp}

    lifecycles:
      FlightLeg:
        initial: scheduled
        persisted_to: aodb.flight_legs
        state_column: leg_status
        states:
          scheduled:
            - {to: gate_out, per_hour: 1.2}
          gate_out:
            - {to: airborne, per_hour: 3.0}
          airborne:
            - {to: landed, per_hour: 0.8}
          landed:
            - {to: gate_in, per_hour: 4.0}
          gate_in:

    seed:
      - table: aodb.aircraft
        count: 3
        columns:
          registration: {generator: id, prefix: G}

    events:
      schedule_published:
        per: aodb.aircraft
        rate_per_hour: 0.25
        emits:
          - table: aodb.flight_legs
            spawns: FlightLeg
            columns:
              leg_id:        {generator: id, prefix: leg}
              registration:  {generator: reference, from: subject.registration}
              leg_status:    {generator: constant, value: scheduled}
              scheduled_out: {generator: now}

      off_blocks:
        lifecycle: FlightLeg
        entering: gate_out
        emits:
          - update: aodb.flight_legs
            where: {leg_id: {generator: reference, from: subject.leg_id}}
            columns: {actual_out: {generator: now}}

      wheels_off:
        lifecycle: FlightLeg
        entering: airborne
        emits:
          - update: aodb.flight_legs
            where: {leg_id: {generator: reference, from: subject.leg_id}}
            columns: {wheels_off: {generator: now}}

      wheels_on:
        lifecycle: FlightLeg
        entering: landed
        emits:
          - update: aodb.flight_legs
            where: {leg_id: {generator: reference, from: subject.leg_id}}
            columns: {wheels_on: {generator: now}}

      on_blocks:
        lifecycle: FlightLeg
        entering: gate_in
        emits:
          - update: aodb.flight_legs
            where: {leg_id: {generator: reference, from: subject.leg_id}}
            columns: {actual_in: {generator: now}}
    """)


@pytest.fixture(scope="module")
def world(tmp_path_factory, postgres_binaries):
    # Shared: every test taking this fixture only reads what it set up.
    # The one that does not -- it ticks the world forward -- takes
    # `moving_world` below instead, so one mutator does not cost the
    # other six a cluster each.
    with running_world(tmp_path_factory.mktemp("world"), OOOI,
                       seed=6, tick_seconds=600, days=3) as built:
        yield built


@pytest.fixture
def moving_world(tmp_path, postgres_binaries):
    """Its own world, for the test that advances the clock."""
    with running_world(tmp_path, OOOI, seed=6, tick_seconds=600, days=3) as built:
        yield built


def legs(world, clause=""):
    return fetch_all(world.silo("aodb"), "aodb", f"SELECT count(*) FROM flight_legs {clause}")[0][0]


# -- the OOOI model --------------------------------------------------

@pytest.mark.postgres
def test_one_row_per_leg_however_many_times_it_is_revised(world):
    # The point of an update rather than four inserts: there is ONE
    # flight, and what changes is what is known about it.
    completed = legs(world, "WHERE actual_in IS NOT NULL")
    assert completed > 10
    assert legs(world) == len(world.living("FlightLeg"))


@pytest.mark.postgres
def test_every_milestone_is_recorded(world):
    # A leg that reached the gate has all four times; one still
    # airborne has only the ones it has passed.
    assert legs(world, "WHERE leg_status = 'gate_in' AND (actual_out IS NULL "
                       "OR wheels_off IS NULL OR wheels_on IS NULL "
                       "OR actual_in IS NULL)") == 0
    assert legs(world, "WHERE leg_status = 'scheduled' AND actual_out IS NOT NULL") == 0


@pytest.mark.postgres
def test_the_milestones_are_in_order(world):
    # Each update runs in the tick its transition happened in, so the
    # simulated clock puts them in sequence. Out-of-order timestamps
    # would mean transitions being replayed or batched.
    completed = legs(world, "WHERE actual_in IS NOT NULL")
    in_order = legs(world,
                    "WHERE actual_in IS NOT NULL "
                    "AND actual_out >= scheduled_out AND wheels_off >= actual_out "
                    "AND wheels_on >= wheels_off AND actual_in >= wheels_on")
    assert in_order == completed > 0


@pytest.mark.postgres
def test_a_transition_fires_its_event_exactly_once(world):
    # Transitions are cleared at the end of every tick. Left in place,
    # each later tick would replay them -- which the timestamps would
    # hide, since the last write wins and still looks plausible.
    rows = fetch_all(world.silo("aodb"), "aodb",
                     "SELECT leg_id, actual_out FROM flight_legs "
                     "WHERE actual_out IS NOT NULL ORDER BY leg_id")
    # A replayed transition would keep moving actual_out forward, so
    # every leg would share the last tick's timestamp.
    stamps = {stamp for _, stamp in rows}
    assert len(stamps) > len(rows) // 2


@pytest.mark.postgres
def test_transitions_are_not_visible_between_ticks(moving_world):
    # The property clearing them actually buys. The first version of
    # this test asserted that a transition fires once, which passed
    # without any clearing at all -- the next tick reassigns the list,
    # so failing to clear has no effect on firing. What clearing
    # guarantees is that a world inspected BETWEEN ticks shows no stale
    # transitions, which is what anything reading the world outside a
    # tick would otherwise trip over.
    assert moving_world.transitions == []
    runner.tick(moving_world, 600)
    assert moving_world.transitions == []


@pytest.mark.postgres
def test_an_update_does_not_leak_its_columns_into_a_later_emission(tmp_path,
                                                                   postgres_binaries):
    # An update builds values on the context's row so a later column
    # can refer to an earlier one, then clears it. Left behind, the
    # next emission in the same occurrence carries those columns into
    # ITS row, and the insert fails against a table that has no such
    # column -- which is how the leak is observable at all.
    #
    # A dedicated pack rather than surgery on OOOI: editing indented
    # YAML by string replacement is fragile enough that the first
    # attempt silently produced a schema with no table in it.
    source = textwrap.dedent("""
        pack: leak

        silos:
          d: {kind: postgresql, database: d}

        schemas:
          d:
            tables:
              jobs:
                columns:
                  job_id: {type: text, length: 64, primary_key: true, nullable: false}
                  status: {type: text, length: 24, nullable: false}
                  done_at: {type: timestamp}
              audit:
                columns:
                  audit_id: {type: text, length: 64, primary_key: true, nullable: false}
                  job_id:   {type: text, length: 64, nullable: false}
              seeds:
                columns:
                  seed_id: {type: text, length: 64, primary_key: true, nullable: false}

        lifecycles:
          Job:
            initial: open
            persisted_to: d.jobs
            state_column: status
            states:
              open:
                - {to: done, per_hour: 4.0}
              done:

        seed:
          - table: d.seeds
            count: 2
            columns:
              seed_id: {generator: id, prefix: s}

        events:
          raise_job:
            per: d.seeds
            rate_per_hour: 3.0
            emits:
              - table: d.jobs
                spawns: Job
                columns:
                  job_id: {generator: id, prefix: job}
                  status: {generator: constant, value: open}

          finish_job:
            lifecycle: Job
            entering: done
            emits:
              - update: d.jobs
                where: {job_id: {generator: reference, from: subject.job_id}}
                columns: {done_at: {generator: now}}
              - table: d.audit
                columns:
                  audit_id: {generator: id, prefix: aud}
                  job_id:   {generator: reference, from: subject.job_id}
        """)

    built = runner.build(write_pack(tmp_path, source, "leak"), tmp_path / "leak", seed=6)
    try:
        runner.seed(built)
        runner.run(built, total_seconds=86400, tick_seconds=600)
        audited = fetch_all(built.silo("d"), "d", "SELECT count(*) FROM audit")[0][0]
        assert audited > 0, "no job ever finished, so nothing was tested"
    finally:
        runner.stop(built)


@pytest.mark.postgres
def test_an_update_only_touches_the_row_it_names(world):
    # Every leg's registration came from its own aircraft at insert and
    # nothing since has written that column, so a `where` matching too
    # broadly would show up as legs sharing one.
    distinct = fetch_all(world.silo("aodb"), "aodb",
                         "SELECT count(DISTINCT registration) FROM flight_legs")[0][0]
    assert distinct == 3


@pytest.mark.postgres
def test_the_status_column_and_the_milestones_agree(world):
    # Two independent writers -- the runner writes leg_status, the
    # events write the times -- so disagreement means one of them is
    # firing on the wrong occasions.
    assert legs(world, "WHERE leg_status IN ('airborne','landed','gate_in') "
                       "AND wheels_off IS NULL") == 0
    assert legs(world, "WHERE leg_status = 'gate_out' AND wheels_off IS NOT NULL") == 0


# -- declaration, checked at load ------------------------------------

def base(event) -> dict:
    return {
        "pack": "x",
        "silos": {"d": {"kind": "postgresql", "database": "d"}},
        "schemas": {"d": {"tables": {"t": {"columns": {
            "i": {"type": "text", "length": 9, "primary_key": True, "nullable": False},
            "s": {"type": "text", "length": 9, "nullable": False},
            "at": {"type": "timestamp"}}}}}},
        "lifecycles": {"L": {
            "initial": "a", "persisted_to": "d.t", "state_column": "s",
            "states": {"a": [{"to": "b", "per_hour": 1.0}], "b": None}}},
        "events": {"e": event},
    }


def valid_update() -> list:
    return [{"update": "d.t",
             "where": {"i": {"generator": "reference", "from": "subject.i"}},
             "columns": {"at": {"generator": "now"}}}]


def test_a_transition_event_loads():
    pack = load_spec(base({"lifecycle": "L", "entering": "b", "emits": valid_update()}))
    assert pack.events[0].trigger.entering == "b"


def test_an_event_cannot_have_both_a_rate_and_a_transition():
    # A transition happens when it happens; a rate would be a second,
    # contradictory answer to how often. The message widened when
    # periodic events made it three ways rather than two.
    with pytest.raises(PackError, match="an event fires one way"):
        load_spec(base({"lifecycle": "L", "entering": "b", "rate_per_hour": 1.0,
                        "emits": valid_update()}))


def test_an_event_needs_one_or_the_other():
    with pytest.raises(PackError, match="needs a rate_per_hour, a lifecycle"):
        load_spec(base({"emits": valid_update()}))


def test_a_transition_event_cannot_also_have_a_per():
    with pytest.raises(PackError, match="not to a `per`"):
        load_spec(base({"lifecycle": "L", "entering": "b", "per": "d.t",
                        "emits": valid_update()}))


def test_an_unknown_lifecycle_is_refused():
    with pytest.raises(PackError, match="no lifecycle called 'Nope'"):
        load_spec(base({"lifecycle": "Nope", "entering": "b", "emits": valid_update()}))


def test_an_unknown_state_is_refused():
    with pytest.raises(PackError, match="has no state 'flying'"):
        load_spec(base({"lifecycle": "L", "entering": "flying", "emits": valid_update()}))


def test_entering_a_state_nothing_reaches_is_refused():
    # An event that could never fire, and silently: a pack doing
    # nothing looks the same as one whose rates are simply low.
    with pytest.raises(PackError, match="could never fire"):
        load_spec(base({"lifecycle": "L", "entering": "a", "emits": valid_update()}))


def test_an_update_needs_a_where():
    # Without one it rewrites every row in the table.
    with pytest.raises(PackError, match="needs a `where`"):
        load_spec(base({"lifecycle": "L", "entering": "b", "emits": [{
            "update": "d.t", "columns": {"at": {"generator": "now"}}}]}))


def test_an_update_needs_columns_to_set():
    with pytest.raises(PackError, match="columns to set"):
        load_spec(base({"lifecycle": "L", "entering": "b", "emits": [{
            "update": "d.t",
            "where": {"i": {"generator": "reference", "from": "subject.i"}}}]}))


def test_an_update_to_a_column_that_does_not_exist_is_refused():
    with pytest.raises(PackError, match="has no column 'landed_at'"):
        load_spec(base({"lifecycle": "L", "entering": "b", "emits": [{
            "update": "d.t",
            "where": {"i": {"generator": "reference", "from": "subject.i"}},
            "columns": {"landed_at": {"generator": "now"}}}]}))


def test_the_transition_subject_carries_the_id_state_and_previous():
    # Named after the persisted table's own key column, so an update
    # addresses the row the way a pack would write it.
    with pytest.raises(PackError, match="the subject table has columns"):
        load_spec(base({"lifecycle": "L", "entering": "b", "emits": [{
            "update": "d.t",
            "where": {"i": {"generator": "reference", "from": "subject.leg_id"}},
            "columns": {"at": {"generator": "now"}}}]}))
    # These three resolve.
    load_spec(base({"lifecycle": "L", "entering": "b", "emits": [{
        "update": "d.t",
        "where": {"i": {"generator": "reference", "from": "subject.i"}},
        "columns": {"s": {"generator": "reference", "from": "subject.previous_state"}}}]}))


@pytest.mark.postgres
def test_the_transitions_own_facts_win_over_the_rows_columns(tmp_path,
                                                             postgres_binaries):
    # The entity's row travels with its transition, so a table with a
    # column called `state` would otherwise shadow the transition's own
    # `state` -- and a pack referring to subject.state would silently
    # get the column instead of the state just entered.
    #
    # Unobservable in the field-service pack, whose work_orders column
    # is called `status`, so the precedence needs a table that collides
    # on purpose before it can be tested at all.
    source = textwrap.dedent("""
        pack: shadow

        silos:
          d: {kind: postgresql, database: d}

        schemas:
          d:
            tables:
              seeds:
                columns:
                  seed_id: {type: text, length: 64, primary_key: true, nullable: false}
              things:
                columns:
                  thing_id: {type: text, length: 64, primary_key: true, nullable: false}
                  status:   {type: text, length: 24, nullable: false}
                  state:    {type: text, length: 24, nullable: false}
                  seen:     {type: text, length: 24}

        lifecycles:
          Thing:
            initial: new
            persisted_to: d.things
            state_column: status
            states:
              new:
                - {to: ready, per_hour: 6.0}
              ready:

        seed:
          - table: d.seeds
            count: 2
            columns:
              seed_id: {generator: id, prefix: s}

        events:
          make_thing:
            per: d.seeds
            rate_per_hour: 4.0
            emits:
              - table: d.things
                spawns: Thing
                columns:
                  thing_id: {generator: id, prefix: t}
                  status:   {generator: constant, value: new}
                  state:    {generator: constant, value: "a column, not the state"}

          record_ready:
            lifecycle: Thing
            entering: ready
            emits:
              - update: d.things
                where: {thing_id: {generator: reference, from: subject.thing_id}}
                columns: {seen: {generator: reference, from: subject.state}}
        """)
    built = runner.build(write_pack(tmp_path, source, "shadow"), tmp_path / "shadow", seed=4)
    try:
        runner.seed(built)
        runner.run(built, total_seconds=43200, tick_seconds=1800)
        rows = fetch_all(built.silo("d"), "d",
                         "SELECT seen FROM things WHERE seen IS NOT NULL")
        assert rows, "nothing became ready, so nothing was tested"
        assert {seen for (seen,) in rows} == {"ready"}
    finally:
        runner.stop(built)
