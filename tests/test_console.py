"""Tests for driving a live world from a prompt.

A simulation you can only configure before it starts is a fixture
generator with extra steps. What is worth pinning here is that the
ground can be made to move while a consumer is attached -- and that
the console says something true about it when it does.
"""

import textwrap

import pytest

from simulator import runner
from simulator.console import COMMANDS, HELP, Console, ConsoleError, ConsoleExit
from simulator.spec import load_pack

PACK = textwrap.dedent("""
    pack: console_shop

    silos:
      ops: {kind: postgresql, database: ops}

    schemas:
      ops:
        tables:
          orders:
            columns:
              order_id: {type: text, length: 64, primary_key: true, nullable: false}
              total:    {type: decimal, precision: 19, scale: 4, nullable: false}
              note:     {type: text, length: 64}
          seeds:
            columns:
              seed_id: {type: text, length: 64, primary_key: true, nullable: false}

    lifecycles:
      Order:
        initial: open
        persisted_to: ops.orders
        state_column: note
        states:
          open:
            - {to: done, per_hour: 0.5}
          done:

    seed:
      - table: ops.seeds
        count: 3
        columns:
          seed_id: {generator: id, prefix: s}

    events:
      order_placed:
        per: ops.seeds
        rate_per_hour: 1.0
        emits:
          - table: ops.orders
            spawns: Order
            columns:
              order_id: {generator: id, prefix: ord}
              total:    {generator: decimal, min: 5, max: 90, scale: 4}
              note:     {generator: constant, value: open}
    """)


@pytest.fixture
def world(tmp_path, postgres_binaries):
    path = tmp_path / "console.yaml"
    path.write_text(PACK)
    built = runner.build(load_pack(path), tmp_path / "var", seed=4)
    runner.seed(built)
    runner.run(built, total_seconds=2 * 86400, tick_seconds=3600)
    try:
        yield built
    finally:
        runner.stop(built)


class Transcript:
    """Drives a console from a list of lines and keeps what it said."""

    def __init__(self, world, lines):
        self.lines = iter(lines)
        self.output: list[str] = []
        self.console = Console(world=world, write=self.output.append,
                               read=self._read)

    def _read(self, _prompt):
        return next(self.lines)

    def run(self):
        self.console.run()
        return "\n".join(self.output)


# -- the loop ---------------------------------------------------------

@pytest.mark.postgres
def test_it_reads_a_scenario_from_a_pipe(world):
    # A scenario is a transcript of what somebody would have typed,
    # rather than a separate format to design.
    printed = Transcript(world, ["status", "advance 1d", "status", "quit"]).run()
    assert printed.count("console_shop") == 2
    assert "advanced to" in printed


@pytest.mark.postgres
def test_a_typo_does_not_kill_the_prompt(world):
    # A console that exits on a typo is worse than useless when a
    # consumer is attached to the world it was holding open.
    printed = Transcript(world, ["wibble", "status", "quit"]).run()
    assert "unknown command 'wibble'" in printed
    assert "console_shop" in printed


@pytest.mark.postgres
def test_a_failing_command_does_not_kill_the_prompt(world):
    printed = Transcript(world, [
        "drift drop_column table=ops.orders column=nope", "status", "quit"]).run()
    assert "error:" in printed
    assert "console_shop" in printed


@pytest.mark.postgres
def test_blank_lines_and_comments_are_skipped(world):
    printed = Transcript(world, ["", "  ", "# a note", "quit"]).run()
    assert printed == ""


@pytest.mark.postgres
def test_end_of_input_stops_it(world):
    # Piping a scenario in ends without a `quit`, and that must be a
    # clean stop rather than a traceback.
    Transcript(world, []).run()


# -- moving time ------------------------------------------------------

@pytest.mark.postgres
def test_advance_moves_the_clock_and_writes_rows(world):
    before = world.clock.now()
    printed = Transcript(world, ["advance 2d", "quit"]).run()
    assert (world.clock.now() - before).days == 2
    assert "rows written" in printed


@pytest.mark.postgres
def test_advance_needs_a_duration(world):
    printed = Transcript(world, ["advance", "advance soon", "quit"]).run()
    assert "needs a duration" in printed
    assert "not a duration" in printed


# -- moving the ground ------------------------------------------------

@pytest.mark.postgres
def test_drift_applies_a_schema_change_now(world):
    from simulator.relational import catalogue_columns

    printed = Transcript(world, [
        "drift add_column table=ops.orders column=channel type=text length=16",
        "quit"]).run()
    assert "added orders.channel" in printed
    assert "channel" in catalogue_columns(world.silo("ops"), "ops", "orders")


@pytest.mark.postgres
def test_drift_uses_the_packs_own_vocabulary(world):
    # `drift add_column table=... column=... type=...` is the migration
    # syntax with `at:` removed, built by the same function the loader
    # calls. Two vocabularies for the same operations would be two to
    # keep in step, and the second would drift.
    from simulator.spec import MIGRATION_OPERATIONS

    printed = Transcript(world, ["drift reticulate table=ops.orders", "quit"]).run()
    for operation in MIGRATION_OPERATIONS:
        assert operation in printed


@pytest.mark.postgres
def test_drift_says_when_it_is_breaking(world):
    printed = Transcript(world, [
        "drift add_column table=ops.orders column=extra type=text length=8",
        "drift drop_column table=ops.orders column=extra",
        "quit"]).run()
    lines = [line for line in printed.splitlines() if line.startswith("applied:")]
    assert "BREAKING" not in lines[0]
    assert "BREAKING" in lines[1]


@pytest.mark.postgres
def test_drift_updates_the_running_schema_so_events_keep_working(world):
    # The world's schema is revised, not the pack's, so the events that
    # follow write the new shape rather than the declared one.
    printed = Transcript(world, [
        "drift add_column table=ops.orders column=channel type=text length=16",
        "advance 1d", "quit"]).run()
    assert "error" not in printed
    assert "channel" in {c.name for c in world.schema("ops").table("orders").columns}


@pytest.mark.postgres
def test_history_shows_what_moved(world):
    printed = Transcript(world, [
        "history",
        "drift rescale_column table=ops.orders column=total factor=100",
        "history", "quit"]).run()
    assert "nothing has drifted yet" in printed
    assert "BREAKING" in printed
    assert "rescaled orders.total by 100" in printed


@pytest.mark.postgres
def test_drift_needs_a_table(world):
    printed = Transcript(world, ["drift drop_column column=note", "quit"]).run()
    assert "needs table=silo.table" in printed


@pytest.mark.postgres
def test_malformed_arguments_are_explained(world):
    printed = Transcript(world, ["drift drop_column ops.orders note", "quit"]).run()
    assert "expected key=value" in printed


# -- judging what moved -----------------------------------------------

@pytest.mark.postgres
def test_a_watch_is_sampled_the_moment_it_is_declared(world):
    printed = Transcript(world, ["watch ops.orders.total", "quit"]).run()
    assert "watching sum(ops.orders.total)" in printed


@pytest.mark.postgres
def test_the_oracle_sees_a_rescale_at_the_moment_it_happens(world):
    # THE point of the console. A first version left the oracle to
    # sample on the next tick, so `drift` followed by `oracle` reported
    # the value from BEFORE the drift -- the most misleading possible
    # answer at the most interesting possible moment.
    printed = Transcript(world, [
        "watch ops.orders.total",
        "oracle",
        "drift rescale_column table=ops.orders column=total factor=100",
        "oracle",
        "quit"]).run()
    reports = [line for line in printed.splitlines() if "sum(ops.orders.total)" in line
               and "watching" not in line]
    assert len(reports) == 2
    assert "hundredfold" not in reports[0]
    assert "hundredfold step(s) -- nothing raised" in reports[1]


@pytest.mark.postgres
def test_the_oracle_reports_a_watch_going_blind(world):
    printed = Transcript(world, [
        "watch ops.orders.total",
        "drift drop_column table=ops.orders column=total",
        "oracle", "quit"]).run()
    assert "went blind" in printed


@pytest.mark.postgres
def test_the_oracle_says_when_nothing_is_watched(world):
    printed = Transcript(world, ["oracle", "quit"]).run()
    assert "nothing is being watched" in printed


@pytest.mark.postgres
def test_a_watch_needs_a_qualified_column(world):
    printed = Transcript(world, ["watch total", "quit"]).run()
    assert "silo.table.column" in printed


# -- the rest ---------------------------------------------------------

@pytest.mark.postgres
def test_status_reports_silos_and_entities(world):
    printed = Transcript(world, ["status", "quit"]).run()
    assert "postgresql" in printed and "up" in printed
    assert "Order" in printed


@pytest.mark.postgres
def test_status_notices_a_silo_that_has_gone_down(world):
    world.silo("ops").terminate()
    printed = Transcript(world, ["status", "quit"]).run()
    assert "DOWN" in printed


@pytest.mark.postgres
def test_connections_reprints_where_to_connect(world):
    printed = Transcript(world, ["connections", "quit"]).run()
    assert "postgresql://127.0.0.1:" in printed


@pytest.mark.postgres
def test_help_lists_every_command(world):
    printed = Transcript(world, ["help", "quit"]).run()
    for name in COMMANDS:
        assert name in printed


def test_every_command_is_documented():
    # A registry and its help text falling out of step is the kind of
    # thing nobody notices until somebody types `help`.
    assert set(COMMANDS) == set(HELP)


def test_quit_stops_the_loop():
    with pytest.raises(ConsoleExit):
        COMMANDS["quit"](None, "")


def test_an_unknown_command_lists_the_real_ones():
    console = Console(world=None, write=lambda _: None, read=lambda _: "")
    with pytest.raises(ConsoleError, match="advance"):
        console.execute("wibble")
