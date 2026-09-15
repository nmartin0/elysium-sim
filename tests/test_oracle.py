"""Tests for the independent record of what was true, and when.

Every drift operation except one eventually throws something, so a
test can assert a consumer failed well. A rescale throws nothing. Its
pass condition is "the numbers are still right", and nothing could say
what right WAS, because the database is the only record and the
database is what moved. These tests are about the second record.
"""

import textwrap
from contextlib import contextmanager
from decimal import Decimal

import pytest

from simulator import runner
from simulator.oracle import Oracle, OracleError, Watch
from simulator.spec import load_pack

TOTAL = Watch(silo="ops", table="orders", column="total", aggregate="sum")
COUNT = Watch(silo="ops", table="orders", column="order_id", aggregate="count")

BASE = """
    pack: watched_shop

    silos:
      ops: {kind: postgresql, database: ops}

    schemas:
      ops:
        tables:
          orders:
            columns:
              order_id: {type: text, length: 64, primary_key: true, nullable: false}
              total:    {type: decimal, precision: 19, scale: 4, nullable: false}
          seeds:
            columns:
              seed_id: {type: text, length: 64, primary_key: true, nullable: false}

    seed:
      - table: ops.seeds
        count: 3
        columns:
          seed_id: {generator: id, prefix: s}

    events:
      order_placed:
        per: ops.seeds
        rate_per_hour: 0.5
        emits:
          - table: ops.orders
            columns:
              order_id: {generator: id, prefix: ord}
              total:    {generator: decimal, min: 10, max: 400, scale: 4}
"""

RESCALED = BASE + """
    migrations:
      - at: 4d
        operation: rescale_column
        table: ops.orders
        column: total
        factor: 100
"""

# A column nothing writes, so dropping it does not break the events --
# which is the case the oracle can actually observe. A pack that drops
# a column its own events use breaks itself first, and loudly, which is
# a different test.
DROPPED = BASE.replace(
    "              total:    {type: decimal, precision: 19, scale: 4, nullable: false}",
    "              total:    {type: decimal, precision: 19, scale: 4, nullable: false}\n"
    "              legacy:   {type: decimal, precision: 19, scale: 4}",
) + """
    migrations:
      - at: 4d
        operation: drop_column
        table: ops.orders
        column: legacy
"""

LEGACY = Watch(silo="ops", table="orders", column="legacy", aggregate="count")


@contextmanager
def run_watched(tmp_path, source, name, watches=(TOTAL,), days=7):
    pack_path = tmp_path / f"{name}.yaml"
    pack_path.write_text(textwrap.dedent(source))
    world = runner.build(load_pack(pack_path), tmp_path / name, seed=8)
    world.oracle = Oracle(watches=watches)
    runner.seed(world)
    try:
        runner.run(world, total_seconds=days * 86400, tick_seconds=3600)
        yield world
    finally:
        runner.stop(world)


@pytest.fixture
def quiet(tmp_path, postgres_binaries):
    # A business with no drift at all: the control every other case in
    # this file is measured against.
    with run_watched(tmp_path, BASE, "quiet") as world:
        yield world


@pytest.fixture
def rescaled(tmp_path, postgres_binaries):
    with run_watched(tmp_path, RESCALED, "rescaled") as world:
        yield world


# -- it records a history --------------------------------------------

@pytest.mark.postgres
def test_it_keeps_a_sample_per_tick(quiet):
    samples = quiet.oracle.series[TOTAL.name]
    assert len(samples) == 7 * 24
    assert samples[0].at < samples[-1].at


@pytest.mark.postgres
def test_it_answers_what_was_true_at_a_moment(quiet):
    samples = quiet.oracle.series[TOTAL.name]
    middle = samples[len(samples) // 2]
    # "As of" rather than "at": a consumer reading between samples saw
    # whatever the last one recorded.
    assert quiet.oracle.at(TOTAL, middle.at) == middle.value
    assert quiet.oracle.at(TOTAL, quiet.clock.start) in (None, samples[0].value)
    assert quiet.oracle.latest(TOTAL) == samples[-1].value


@pytest.mark.postgres
def test_a_quiet_business_only_ever_grows(quiet):
    # Orders accumulate and nothing removes them, so the total is
    # monotonic. This is what "normal" looks like, and it is what makes
    # the rescale step meaningful rather than just large.
    #
    # The early samples are None because SUM over an empty table is
    # NULL -- the business has not traded yet, which is not the same
    # as the watch failing. That distinction is what Sample.ok carries.
    values = [sample.value for sample in quiet.oracle.series[TOTAL.name]
              if sample.value is not None]
    assert len(values) > 24
    assert all(later >= earlier for earlier, later in zip(values, values[1:], strict=False))


# -- the silent one ---------------------------------------------------

@pytest.mark.postgres
def test_a_rescale_shows_up_as_a_step_nothing_else_explains(rescaled):
    # THE case the oracle exists for. Nothing raised, every read
    # succeeded, every type still checked -- and the total multiplied
    # by a hundred between two consecutive hours.
    jumps = rescaled.oracle.jumps(TOTAL, Decimal(100))
    assert len(jumps) == 1
    assert jumps[0].at.day == 5  # the tick after the day-4 migration


@pytest.mark.postgres
def test_a_business_without_a_migration_has_no_such_step(quiet):
    # The negative that gives the positive its meaning. A hundredfold
    # step is not something a business does.
    assert quiet.oracle.jumps(TOTAL, Decimal(100)) == []


@pytest.mark.postgres
def test_what_a_consumer_read_before_the_rescale_still_matches_the_oracle(rescaled):
    # The question a consumer's own test would ask: I cached this
    # number on day two; was I right at the time? The oracle is the
    # referee, and it is not the database, which has since moved.
    samples = rescaled.oracle.series[TOTAL.name]
    before = samples[24 * 2]
    assert rescaled.oracle.at(TOTAL, before.at) == before.value
    assert rescaled.oracle.latest(TOTAL) > before.value * 50


@pytest.mark.postgres
def test_the_step_is_never_exactly_the_factor(rescaled):
    # Because the simulation keeps writing at the OLD scale between the
    # two samples, so the column holds two units at once. That
    # inexactness is the mark of the botched migration rather than a
    # clean one, and is why jumps() compares ratios with a tolerance.
    samples = rescaled.oracle.series[TOTAL.name]
    jump = rescaled.oracle.jumps(TOTAL, Decimal(100))[0]
    index = [sample.at for sample in samples].index(jump.at)
    ratio = Decimal(str(jump.value)) / Decimal(str(samples[index - 1].value))
    assert ratio != Decimal(100)
    assert Decimal(90) < ratio < Decimal(110)


# -- the loud one, for contrast ---------------------------------------

@pytest.mark.postgres
def test_a_dropped_column_makes_a_watch_go_blind(tmp_path, postgres_binaries):
    # A failed sample is recorded as None rather than raised: a watch
    # whose column has gone is exactly what the oracle exists to
    # observe, and refusing to sample would turn the instrument off at
    # the moment it became interesting.
    with run_watched(tmp_path, DROPPED, "dropped", watches=(TOTAL, LEGACY),
                     days=5) as world:
        blind_at = world.oracle.went_blind(LEGACY)
        assert blind_at is not None
        assert blind_at.day == 5
        # And the watch on a column that is still there keeps working,
        # so going blind is about the column rather than the silo.
        assert world.oracle.went_blind(TOTAL) is None
        assert world.oracle.latest(TOTAL) is not None


@pytest.mark.postgres
def test_a_watch_that_never_fails_never_goes_blind(quiet):
    assert quiet.oracle.went_blind(TOTAL) is None


@pytest.mark.postgres
def test_an_empty_table_is_not_a_blind_watch(tmp_path, postgres_binaries):
    # THE distinction Sample.ok exists for, and the `quiet` world above
    # cannot test it: by its first sample the shop has already traded,
    # so the value is never None and the assertion holds whatever
    # went_blind does. A world sampled before anything happened has a
    # SUM of NULL -- which is not the same as a watch that cannot run.
    pack_path = tmp_path / "idle.yaml"
    pack_path.write_text(textwrap.dedent(BASE))
    world = runner.build(load_pack(pack_path), tmp_path / "idle", seed=8)
    world.oracle = Oracle(watches=(TOTAL,))
    runner.seed(world)
    try:
        runner.tick(world, 1)
        samples = world.oracle.series[TOTAL.name]
        assert samples[0].value is None, "the shop traded in one second"
        assert samples[0].ok is True
        assert world.oracle.went_blind(TOTAL) is None
    finally:
        runner.stop(world)


# -- several watches --------------------------------------------------

@pytest.mark.postgres
def test_watches_are_independent(tmp_path, postgres_binaries):
    with run_watched(tmp_path, RESCALED, "two", watches=(TOTAL, COUNT), days=6) as world:
        # The rescale moved the money and left the row count alone,
        # which is what makes it invisible to anything counting rows.
        assert world.oracle.jumps(TOTAL, Decimal(100))
        assert world.oracle.jumps(COUNT, Decimal(100)) == []
        assert world.oracle.latest(COUNT) > 0


# -- declaration ------------------------------------------------------

def test_a_watch_checks_its_aggregate():
    with pytest.raises(OracleError, match="not an aggregate"):
        Watch(silo="ops", table="orders", column="total", aggregate="median")


def test_a_watch_names_itself_readably():
    assert TOTAL.name == "sum(ops.orders.total)"


def test_an_oracle_with_no_watches_records_nothing():
    # A world nobody is checking should not pay a query per tick.
    oracle = Oracle()
    assert oracle.series == {}
    assert oracle.latest(TOTAL) is None
    assert oracle.jumps(TOTAL, Decimal(100)) == []
