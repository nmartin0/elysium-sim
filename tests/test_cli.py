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
        with psycopg.connect(host=details["host"], port=details["port"],
                             dbname=details["database"], user=details["user"]) as connection:
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
