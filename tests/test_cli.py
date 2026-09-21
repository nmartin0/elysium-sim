"""Tests for running a simulation without writing a script.

The product is "point your consumer at this", so these tests are about
whether somebody who has never imported the package can get a database
to connect to, and whether what they are told about it is true.
"""

import json
import pathlib
import textwrap

import pytest

from simulator.cli import CONNECTIONS_FILENAME, main

PACK = textwrap.dedent("""
    pack: tiny_shop
    description: One till.

    silos:
      ops:  {kind: postgresql, database: ops}
      drop: {kind: filedrop}

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
        states:
          open:
            - {to: done, per_hour: 1.0}
          done:

    seed:
      - table: ops.seeds
        count: 2
        columns:
          seed_id: {generator: id, prefix: s}

    events:
      order_placed:
        per: ops.seeds
        rate_per_hour: 1.0
        emits:
          - table: ops.orders
            columns:
              order_id: {generator: id, prefix: ord}
              total:    {generator: decimal, min: 5, max: 90, scale: 4}

    migrations:
      - at: 2d
        operation: add_column
        table: ops.orders
        column: channel
        type: text
        length: 16
    """)


@pytest.fixture
def pack_file(tmp_path):
    path = tmp_path / "tiny.yaml"
    path.write_text(PACK)
    return path


# -- check ------------------------------------------------------------

def test_check_accepts_a_good_pack(pack_file, capsys):
    assert main(["check", str(pack_file)]) == 0
    printed = capsys.readouterr().out
    assert "tiny_shop" in printed
    # It reports what the pack contains, so somebody can see at a
    # glance whether the file says what they meant.
    assert "postgresql" in printed and "filedrop" in printed
    assert "Order" in printed
    assert "order_placed" in printed
    assert "day 2" in printed


def test_check_refuses_a_bad_pack_with_its_path(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text(PACK.replace("kind: postgresql", "kind: oracle"))
    assert main(["check", str(bad)]) == 1
    # On stderr, so `simulator check` can be used in a pipeline.
    errors = capsys.readouterr().err
    assert "unknown silo kind 'oracle'" in errors
    assert "silos.ops" in errors


def test_check_on_a_missing_file_does_not_traceback(tmp_path, capsys):
    assert main(["check", str(tmp_path / "nope.yaml")]) == 1
    assert "nope.yaml" in capsys.readouterr().err


def test_check_builds_nothing(pack_file, tmp_path):
    main(["check", str(pack_file)])
    # Fast enough to run on every save, which it is only if it never
    # starts a database.
    assert [entry.name for entry in tmp_path.iterdir()] == ["tiny.yaml"]


# -- run --------------------------------------------------------------

@pytest.mark.postgres
def test_run_builds_simulates_and_tears_down(pack_file, tmp_path, capsys,
                                             postgres_binaries):
    directory = tmp_path / "var"
    assert main(["run", str(pack_file), "--dir", str(directory),
                 "--days", "3", "--seed", "5", "--stop-after"]) == 0
    printed = capsys.readouterr().out
    assert "Simulating 3 days" in printed
    assert "rows written" in printed
    assert "postgresql://127.0.0.1:" in printed


@pytest.mark.postgres
def test_connections_are_written_where_a_consumer_can_read_them(
        pack_file, tmp_path, postgres_binaries):
    # A port copied out of a terminal is a port somebody mistypes.
    directory = tmp_path / "var"
    main(["run", str(pack_file), "--dir", str(directory),
          "--days", "1", "--stop-after"])

    written = json.loads((directory / CONNECTIONS_FILENAME).read_text())
    assert written["pack"] == "tiny_shop"
    assert set(written["silos"]) == {"ops", "drop"}

    ops = written["silos"]["ops"]
    # Everything a driver needs, and the BUSINESS database rather than
    # the maintenance one -- a consumer following the wrong one
    # connects successfully and finds nothing.
    assert ops["kind"] == "postgresql"
    assert ops["database"] == "ops"
    assert ops["host"] == "127.0.0.1"
    assert isinstance(ops["port"], int)
    assert ops["user"]

    drop = written["silos"]["drop"]
    assert drop["kind"] == "filedrop"
    # Including the encoding, because these files carry a BOM and a
    # consumer reading plain utf-8 gets three stray bytes.
    assert drop["encoding"] == "utf-8-sig"


@pytest.mark.postgres
def test_connections_appear_before_the_simulation_does(pack_file, tmp_path,
                                                       monkeypatch, postgres_binaries):
    # Written up front so a consumer waiting on the file can connect
    # during the backfill rather than after it.
    #
    # Checked by forcing the interleaving. A first version asserted the
    # file existed once the run had finished, which is true whether it
    # was written first or last -- the teardown writes it again either
    # way. The only moment that distinguishes them is while the
    # simulation is still going.
    from simulator import cli

    directory = tmp_path / "var"
    seen = []
    original = cli.runner.seed

    def observe(world):
        seen.append((directory / CONNECTIONS_FILENAME).exists())
        return original(world)

    monkeypatch.setattr(cli.runner, "seed", observe)
    main(["run", str(pack_file), "--dir", str(directory),
          "--days", "1", "--stop-after"])

    assert seen == [True], "the world was seeded before anyone could connect to it"


@pytest.mark.postgres
def test_the_connections_file_is_never_visible_half_written(
        pack_file, tmp_path, monkeypatch, postgres_binaries):
    # Written to a temporary name and renamed, for the same reason the
    # file-drop silo publishes that way: a consumer polling for this
    # file must not read half of it.
    #
    # A first version asserted no `.part` file was left behind, which
    # writing directly to the final name also satisfies -- it was
    # testing the outcome both implementations share. Every write is
    # intercepted instead, and the payload must never land on the
    # published name.
    import pathlib as _pathlib

    directory = tmp_path / "var"
    original = _pathlib.Path.write_text
    written = []

    def observe(self, *args, **kwargs):
        if self.name.startswith(CONNECTIONS_FILENAME.split(".")[0]):
            written.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(_pathlib.Path, "write_text", observe)
    main(["run", str(pack_file), "--dir", str(directory),
          "--days", "1", "--stop-after"])

    assert written, "nothing was written"
    for name in written:
        assert name.endswith(".part"), name
    assert (directory / CONNECTIONS_FILENAME).exists()
    assert list(directory.glob("*.part")) == []


@pytest.mark.postgres
def test_a_consumer_can_read_the_world_using_only_that_file(
        pack_file, tmp_path, postgres_binaries):
    # THE test this whole command exists for: connect knowing nothing
    # except what was written to disk, with no reference to the world
    # object or the pack.
    import psycopg

    from simulator import runner
    from simulator.spec import load_pack

    directory = tmp_path / "var"
    world = runner.build(load_pack(pack_file), directory, seed=5)
    try:
        from simulator.cli import write_connections
        write_connections(world, directory)
        runner.seed(world)
        runner.run(world, total_seconds=2 * 86400, tick_seconds=3600)

        details = json.loads((directory / CONNECTIONS_FILENAME).read_text())["silos"]["ops"]
        # The password comes from the same file -- which is the point:
        # everything a consumer needs is in one place, credential
        # included.
        with psycopg.connect(host=details["host"], port=details["port"],
                             dbname=details["database"], user=details["user"],
                             password=details["password"]) as connection:
            count = connection.execute("SELECT count(*) FROM orders").fetchone()[0]
        assert count > 0
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_stop_after_really_stops(pack_file, tmp_path, postgres_binaries):
    # A leaked cluster holds its port, so the next run of the same
    # world fails on a conflict unrelated to whatever went wrong.
    import socket

    directory = tmp_path / "var"
    main(["run", str(pack_file), "--dir", str(directory),
          "--days", "1", "--stop-after"])
    port = json.loads((directory / CONNECTIONS_FILENAME).read_text())["silos"]["ops"]["port"]

    probe = socket.socket()
    probe.settimeout(2)
    try:
        with pytest.raises(OSError):
            probe.connect(("127.0.0.1", port))
    finally:
        probe.close()


@pytest.mark.postgres
def test_the_same_seed_gives_the_same_world_from_the_command_line(
        pack_file, tmp_path, postgres_binaries, capsys):
    def rows_for(name):
        main(["run", str(pack_file), "--dir", str(tmp_path / name),
              "--days", "2", "--seed", "9", "--stop-after"])
        printed = capsys.readouterr().out
        return [line for line in printed.splitlines() if "rows written" in line][0]

    assert rows_for("a") == rows_for("b")


@pytest.mark.postgres
def test_drift_declared_in_the_pack_happens_under_the_command(
        pack_file, tmp_path, postgres_binaries, capsys):
    directory = tmp_path / "var"
    main(["run", str(pack_file), "--dir", str(directory),
          "--days", "1", "--stop-after"])
    assert "rows written" in capsys.readouterr().out

    later = tmp_path / "later"
    main(["run", str(pack_file), "--dir", str(later),
          "--days", "4", "--stop-after"])
    # The pack adds a column on day two; nothing here asked for it.
    assert "rows written" in capsys.readouterr().out


def test_piping_into_head_does_not_traceback(pack_file):
    # `simulator check pack.yaml | head -2` is an ordinary thing to do.
    # Without handling, the closed pipe ends in a traceback that looks
    # like the PACK was broken rather than the pipe -- and then a
    # second one at interpreter shutdown, when Python flushes stdout.
    import subprocess
    import sys as _sys

    check = subprocess.Popen(
        [_sys.executable, "-m", "simulator", "check", str(pack_file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(pathlib.Path(__file__).resolve().parent.parent),
    )
    head = subprocess.Popen(["head", "-2"], stdin=check.stdout,
                            stdout=subprocess.DEVNULL)
    check.stdout.close()
    head.wait()
    errors = check.stderr.read().decode()
    check.wait()

    assert "BrokenPipeError" not in errors, errors
    assert "Traceback" not in errors, errors


# -- argument handling -------------------------------------------------

def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit):
        main([])


def test_an_unknown_command_is_refused(capsys):
    with pytest.raises(SystemExit):
        main(["simulate", "x.yaml"])


# -- attaching to a world somebody else is running ---------------------

@pytest.fixture
def running(pack_file, tmp_path, postgres_binaries):
    """A world built and left up, as `run` would leave it."""
    from simulator import runner
    from simulator.cli import write_connections
    from simulator.spec import load_pack

    directory = tmp_path / "var"
    world = runner.build(load_pack(pack_file), directory, seed=5)
    write_connections(world, directory)
    runner.seed(world)
    runner.run(world, total_seconds=2 * 86400, tick_seconds=3600)
    try:
        yield world, directory
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_status_describes_a_world_it_did_not_build(running, capsys):
    # Nothing is shared with the running process except the files it
    # published: no world object, no pack.
    _, directory = running
    assert main(["status", "--dir", str(directory)]) == 0
    printed = capsys.readouterr().out
    assert "tiny_shop" in printed
    assert "postgresql" in printed and "up" in printed
    assert "orders" in printed


@pytest.mark.postgres
def test_status_reads_the_schema_from_the_engine(running, capsys):
    # Not from the pack, which stops being true the moment anything
    # drifts.
    _, directory = running
    main(["drift", "add_column", "table=ops.orders", "column=channel",
          "type=text", "length=16", "--dir", str(directory)])
    capsys.readouterr()

    main(["status", "--dir", str(directory)])
    # On the TABLE line specifically. A first version just looked for
    # the name anywhere in the output, which the drift history line
    # also contains -- so it passed against a status that listed no
    # tables at all.
    tables = [line for line in capsys.readouterr().out.splitlines()
              if line.strip().startswith("table  orders")]
    assert tables and "channel" in tables[0]


@pytest.mark.postgres
def test_status_says_nothing_about_drift_before_any_happens(pack_file, tmp_path,
                                                            capsys, postgres_binaries):
    # No history table is an ordinary state, and used to end in a
    # traceback. Needs a world that has NOT yet drifted, so it cannot
    # use the shared fixture -- that pack adds a column on day two and
    # the fixture runs for two days.
    from simulator import runner
    from simulator.cli import write_connections
    from simulator.spec import load_pack

    directory = tmp_path / "fresh"
    world = runner.build(load_pack(pack_file), directory, seed=5)
    write_connections(world, directory)
    try:
        assert main(["status", "--dir", str(directory)]) == 0
        printed = capsys.readouterr()
        assert "Traceback" not in printed.out + printed.err
        assert "drift" not in printed.out
        assert "orders" in printed.out
    finally:
        runner.stop(world)


@pytest.mark.postgres
def test_status_notices_a_silo_that_is_down(running, capsys):
    world, directory = running
    world.silo("ops").terminate()
    main(["status", "--dir", str(directory)])
    assert "DOWN" in capsys.readouterr().out


@pytest.mark.postgres
def test_drift_changes_a_running_worlds_schema_from_outside(running, capsys):
    # The case worth having: a consumer is attached and you want to
    # move the ground under it without stopping anything.
    from simulator.relational import catalogue_columns

    world, directory = running
    assert main(["drift", "drop_column", "table=ops.orders", "column=note",
                 "--dir", str(directory)]) == 0
    printed = capsys.readouterr().out
    assert "dropped orders.note" in printed
    assert "BREAKING" in printed
    # Against the ENGINE, which is what a consumer would see.
    assert "note" not in catalogue_columns(world.silo("ops"), "ops", "orders")


@pytest.mark.postgres
def test_drift_from_outside_warns_that_the_runner_is_now_wrong(running, capsys):
    # The one thing it cannot do: the running process holds its schema
    # in memory and has just been made wrong about it. Saying so is
    # better than letting somebody find out.
    _, directory = running
    main(["drift", "drop_column", "table=ops.orders", "column=note",
          "--dir", str(directory)])
    assert "still believes the old schema" in capsys.readouterr().out


@pytest.mark.postgres
def test_drift_from_outside_is_recorded_in_the_history(running, capsys):
    _, directory = running
    main(["drift", "rescale_column", "table=ops.orders", "column=total",
          "factor=100", "--dir", str(directory)])
    capsys.readouterr()

    main(["status", "--dir", str(directory)])
    printed = capsys.readouterr().out
    assert "BREAKING" in printed
    assert "rescaled orders.total by 100" in printed


@pytest.mark.postgres
def test_drift_uses_the_same_vocabulary_as_a_pack(running, capsys):
    from simulator.spec import MIGRATION_OPERATIONS

    _, directory = running
    assert main(["drift", "reticulate", "table=ops.orders",
                 "--dir", str(directory)]) == 1
    errors = capsys.readouterr().err
    for operation in MIGRATION_OPERATIONS:
        assert operation in errors


@pytest.mark.postgres
def test_drift_refuses_a_silo_that_holds_no_database(running, capsys):
    _, directory = running
    assert main(["drift", "drop_column", "table=drop.orders", "column=x",
                 "--dir", str(directory)]) == 1
    assert "holds no database" in capsys.readouterr().err


@pytest.mark.postgres
def test_drift_refuses_a_silo_that_is_not_there(running, capsys):
    _, directory = running
    assert main(["drift", "drop_column", "table=ghost.orders", "column=x",
                 "--dir", str(directory)]) == 1
    assert "no silo called 'ghost'" in capsys.readouterr().err


@pytest.mark.postgres
def test_drift_needs_a_qualified_table(running, capsys):
    _, directory = running
    assert main(["drift", "drop_column", "table=orders", "column=x",
                 "--dir", str(directory)]) == 1
    assert "table=silo.table" in capsys.readouterr().err


@pytest.mark.postgres
def test_malformed_arguments_are_explained(running, capsys):
    _, directory = running
    assert main(["drift", "drop_column", "ops.orders", "--dir", str(directory)]) == 1
    assert "expected key=value" in capsys.readouterr().err


def test_attaching_to_nothing_says_so(tmp_path, capsys):
    assert main(["status", "--dir", str(tmp_path)]) == 1
    assert "is a world running there" in capsys.readouterr().err
    assert main(["drift", "drop_column", "table=a.b", "column=c",
                 "--dir", str(tmp_path)]) == 1
    assert "is a world running there" in capsys.readouterr().err


# -- proving the trainer is sound -------------------------------------

@pytest.mark.postgres
def test_verify_passes_on_a_healthy_world(running, capsys):
    # WHY THIS EXISTS. Somebody learning to connect a tool to these
    # databases will hit a problem, and their first question is whether
    # the fault is theirs or the trainer's. Without an answer they
    # spend the afternoon in the wrong logs.
    _, directory = running
    assert main(["verify", "--dir", str(directory)]) == 0
    printed = capsys.readouterr().out
    assert "All sound" in printed
    for check in ("reachable", "tables present and populated",
                  "every table has a primary key", "cannot write"):
        assert check in printed, printed


@pytest.mark.postgres
def test_verify_says_so_when_a_silo_is_down(running, capsys):
    # The answer that matters most: it is not you.
    world, directory = running
    world.silo("ops").terminate()

    assert main(["verify", "--dir", str(directory)]) == 1
    printed = capsys.readouterr().out
    assert "FAILED" in printed
    assert "The fault is in the silos" in printed


@pytest.mark.postgres
def test_verify_notices_a_read_account_that_can_write(running, monkeypatch, capsys):
    # The check that would be easiest to leave passing by accident,
    # because the happy path looks identical whether or not the DELETE
    # was actually attempted.

    world, directory = running
    with world.silo("ops").connect("ops", autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute('GRANT DELETE ON ALL TABLES IN SCHEMA public TO "reader"')

    assert main(["verify", "--dir", str(directory)]) == 1
    assert "was allowed to DELETE" in capsys.readouterr().out


@pytest.mark.postgres
def test_verify_notices_a_table_with_no_primary_key(running, capsys):
    # A first version of this file asserted only that the check's NAME
    # appeared and the run passed, which is equally true of a check
    # that looks at nothing -- and the control aimed at it did not
    # fire. A table without a key has to actually be there.
    world, directory = running
    with world.silo("ops").connect("ops", autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute('CREATE TABLE "loose_notes" (note text)')
            cursor.execute('INSERT INTO "loose_notes" VALUES (\'x\')')
            cursor.execute('GRANT SELECT ON "loose_notes" TO "reader"')

    assert main(["verify", "--dir", str(directory)]) == 1
    assert "no primary key on ['loose_notes']" in capsys.readouterr().out


@pytest.mark.postgres
def test_verify_uses_only_the_published_file(tmp_path, capsys):
    # No world object and no pack: if connections.json is not enough,
    # that is the finding.
    assert main(["verify", "--dir", str(tmp_path)]) == 1
    assert "is a world running there" in capsys.readouterr().err


@pytest.mark.postgres
def test_verify_does_not_call_a_broken_delete_a_refusal(running, monkeypatch,
                                                        capsys):
    # A check that can pass for the wrong reason is worse than no
    # check, because somebody believes it. Any exception used to count
    # as "refused, as it should be" -- so a missing table, a dropped
    # connection or a typo reported a security guarantee that had not
    # been tested at all.
    from simulator import health

    _, directory = running
    monkeypatch.setattr(
        health, "_is_permission_error", lambda error: False)

    assert main(["verify", "--dir", str(directory)]) == 1
    printed = capsys.readouterr().out
    assert "not because it was refused" in printed


def test_a_refusal_is_told_apart_from_a_failure():
    from simulator.health import _is_permission_error

    for refusal in ("permission denied for table work_orders",
                    "must be owner of table x",
                    "(1142, \"DELETE command denied to user 'reader'\")",
                    "Access denied for user 'reader'@'127.0.0.1'"):
        assert _is_permission_error(Exception(refusal)), refusal

    for failure in ('relation "pay_lines" does not exist',
                    "server closed the connection unexpectedly",
                    "syntax error at or near DELETEE"):
        assert not _is_permission_error(Exception(failure)), failure
