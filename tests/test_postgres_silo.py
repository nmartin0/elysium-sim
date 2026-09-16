"""Tests for PostgreSQL instance supervision.

Split in two. The discovery and refusal tests need no server and always
run. The lifecycle tests start a real instance, are marked `postgres`,
and skip cleanly where one is unavailable -- including as root, which
PostgreSQL refuses outright.
"""

import os
from pathlib import Path

import pytest

from simulator.ports import PortRegistry
from simulator.silo import SiloError
from simulator.silos.postgres import (
    MAINTENANCE_DATABASE,
    PostgresBinaries,
    PostgresSilo,
    PostgresUnavailable,
    refuse_if_root,
)

# -- discovery, no server needed -------------------------------------


def test_discovery_finds_binaries_on_this_machine():
    # May legitimately fail to find anything, which is itself the
    # documented behaviour -- so both outcomes are accepted, and what is
    # asserted is that it does not do something in between.
    try:
        binaries = PostgresBinaries.discover()
    except PostgresUnavailable as error:
        assert "initdb" in str(error) or "pg_ctl" in str(error)
        assert "PATH" in str(error)
        return
    assert binaries.initdb.name == "initdb"
    assert binaries.pg_ctl.name == "pg_ctl"
    assert os.access(binaries.initdb, os.X_OK)


def test_discovery_prefers_path_over_packaged(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    for name in ("initdb", "pg_ctl"):
        target = fake / name
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake))
    binaries = PostgresBinaries.discover()
    assert binaries.initdb == fake / "initdb"


def test_discovery_finds_a_debian_style_packaged_install(tmp_path, monkeypatch):
    # THE case that motivates the fallback, and the one the machine this
    # suite runs on cannot exercise by accident: PATH has nothing, and a
    # perfectly good install sits under /usr/lib/postgresql/<major>/bin
    # because Debian and Ubuntu deliberately keep it off PATH so several
    # majors can coexist.
    packaged = tmp_path / "usr/lib/postgresql/16/bin"
    packaged.mkdir(parents=True)
    for name in ("initdb", "pg_ctl"):
        target = packaged / name
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr("simulator.silos.postgres._PACKAGED_BIN_GLOB",
                        str(tmp_path / "usr/lib/postgresql/*/bin"))
    binaries = PostgresBinaries.discover()
    assert binaries.initdb == packaged / "initdb"
    assert binaries.pg_ctl == packaged / "pg_ctl"


def test_discovery_prefers_the_newest_major(tmp_path, monkeypatch):
    for major in ("14", "16"):
        directory = tmp_path / f"usr/lib/postgresql/{major}/bin"
        directory.mkdir(parents=True)
        for name in ("initdb", "pg_ctl"):
            target = directory / name
            target.write_text("#!/bin/sh\n")
            target.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr("simulator.silos.postgres._PACKAGED_BIN_GLOB",
                        str(tmp_path / "usr/lib/postgresql/*/bin"))
    assert "16" in str(PostgresBinaries.discover().initdb)


def test_discovery_ignores_a_non_executable_file(tmp_path, monkeypatch):
    # A leftover or a package mid-install. Treating it as usable would
    # produce a permission error from inside a subprocess much later.
    directory = tmp_path / "usr/lib/postgresql/16/bin"
    directory.mkdir(parents=True)
    (directory / "initdb").write_text("")
    (directory / "pg_ctl").write_text("")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr("simulator.silos.postgres._PACKAGED_BIN_GLOB",
                        str(tmp_path / "usr/lib/postgresql/*/bin"))
    with pytest.raises(PostgresUnavailable):
        PostgresBinaries.discover()


def test_discovery_explains_itself_when_nothing_is_installed(tmp_path, monkeypatch):
    # Debian and Ubuntu install to /usr/lib/postgresql/<major>/bin and
    # leave it off PATH, so the message has to mention both places or it
    # sends someone looking in the wrong one.
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("simulator.silos.postgres._PACKAGED_BIN_GLOB", str(tmp_path / "nothing/*/bin"))
    with pytest.raises(PostgresUnavailable) as raised:
        PostgresBinaries.discover()
    assert "PATH" in str(raised.value)
    assert "Install PostgreSQL" in str(raised.value)


def test_root_is_refused_with_an_actionable_message(monkeypatch):
    # PostgreSQL refuses root itself, but only after the caller has
    # committed to a world directory and from inside a subprocess trace.
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(SiloError) as raised:
        refuse_if_root()
    assert "root" in str(raised.value)
    assert "ordinary user" in str(raised.value)


def test_non_root_passes_the_check(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    refuse_if_root()


# -- paths, no server needed -----------------------------------------


def _server(tmp_path: Path, port: int = 5999) -> PostgresSilo:
    return PostgresSilo(
        name="pos",
        data_dir=tmp_path / "pos",
        port=port,
        binaries=PostgresBinaries(initdb=Path("/nonexistent/initdb"),
                                  pg_ctl=Path("/nonexistent/pg_ctl")),
    )


def test_connection_kwargs_default_to_the_maintenance_database(tmp_path):
    # initdb -U sim creates the ROLE sim, not a database called sim.
    # Assuming otherwise looks like a working configuration right up
    # until something connects, which is exactly how this was found.
    server = _server(tmp_path, port=6001)
    assert _server(tmp_path).connection_kwargs()["dbname"] == MAINTENANCE_DATABASE
    assert server.connection_kwargs("pos")["dbname"] == "pos"
    assert server.connection_kwargs()["host"] == "127.0.0.1"
    assert server.connection_kwargs()["port"] == 6001


def test_there_is_no_unix_socket_to_outgrow_its_path_limit(tmp_path):
    # The socket used to live inside the world, to avoid /tmp
    # collisions. That ran into a harder constraint: sun_path is 108
    # bytes, and a world a few directories deep exceeds it -- the
    # server refused to start with "Unix-domain socket path is too
    # long (maximum 107 bytes)". Every connection here is TCP, so the
    # socket was pure liability and is gone.
    assert not hasattr(_server(tmp_path), "socket_dir")


def test_starting_without_a_cluster_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(SiloError, match="no cluster"):
        _server(tmp_path).start()


def test_is_running_is_false_without_a_pid_file(tmp_path):
    assert _server(tmp_path).is_reachable() is False


def test_a_pid_file_naming_a_dead_process_reads_as_not_running(tmp_path):
    # The stale-pid case, which is what pg_ctl refuses to start over
    # with "another server might be running". Recognising it is what
    # lets _clear_stale_pid remove it safely.
    server = _server(tmp_path)
    server.cluster_dir.mkdir(parents=True)
    # A pid that cannot exist: above the system maximum.
    server.pid_path.write_text("4194305\n")
    assert server.is_reachable() is False


def test_a_corrupt_pid_file_reads_as_not_running(tmp_path):
    server = _server(tmp_path)
    server.cluster_dir.mkdir(parents=True)
    server.pid_path.write_text("not a pid\n")
    assert server.is_reachable() is False


def test_stopping_something_that_is_not_running_is_quiet(tmp_path):
    # Called on every teardown path, including ones where the server
    # already died. Raising there would mask the original failure.
    _server(tmp_path).stop()


def test_terminate_on_a_dead_server_is_quiet(tmp_path):
    _server(tmp_path).terminate()


# -- the real thing --------------------------------------------------


@pytest.fixture
def running_server(tmp_path, postgres_binaries):
    registry = PortRegistry.allocate(tmp_path, ["pos"])
    server = PostgresSilo(name="pos", data_dir=tmp_path / "pos",
                            port=registry.port("pos"), binaries=postgres_binaries)
    server.create()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.mark.postgres
def test_an_instance_starts_and_accepts_connections(running_server):
    import psycopg

    assert running_server.is_reachable()
    with psycopg.connect(**running_server.connection_kwargs(), connect_timeout=10) as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1


@pytest.mark.postgres
def test_it_listens_only_on_loopback(running_server):
    # A simulated world answering on a LAN interface would be a
    # genuinely bad thing to leave running.
    import psycopg

    with psycopg.connect(**running_server.connection_kwargs()) as connection:
        listening = connection.execute("SHOW listen_addresses").fetchone()[0]
    assert listening == "127.0.0.1"


@pytest.mark.postgres
def test_stop_then_start_again(running_server):
    running_server.stop()
    assert not running_server.is_reachable()
    running_server.start()
    assert running_server.is_reachable()


@pytest.mark.postgres
def test_a_stale_pid_file_does_not_block_a_restart(running_server):
    # The situation after a reboot or a kill. This pins an assumption
    # about pg_ctl rather than about our code: pg_ctl detects a stale
    # pid file and starts anyway. We used to clear it ourselves; a
    # negative control showed that code was doing nothing, so it was
    # removed and this test kept, because the assumption is load-bearing
    # and would break silently if a future pg_ctl changed its mind.
    running_server.stop()
    running_server.pid_path.write_text("4194305\n")
    running_server.start()
    assert running_server.is_reachable()


@pytest.mark.postgres
def test_terminate_makes_the_port_stop_answering(running_server):
    # "The database went away", made to happen rather than mocked. With
    # a file-backed database this was hard to simulate honestly, because
    # the next read recreates the file; a killed server simply stops.
    import psycopg

    running_server.terminate()
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(**running_server.connection_kwargs(), connect_timeout=5)


@pytest.mark.postgres
def test_two_instances_coexist_on_their_own_ports(tmp_path, postgres_binaries):
    # What a multi-silo world is: separate systems, separate failure
    # domains, individually killable.
    import psycopg

    registry = PortRegistry.allocate(tmp_path, ["pos", "books"])
    servers = []
    try:
        for name in ("pos", "books"):
            server = PostgresSilo(name=name, data_dir=tmp_path / name,
                                    port=registry.port(name), binaries=postgres_binaries)
            server.create()
            server.start()
            servers.append(server)
        for server in servers:
            with psycopg.connect(**server.connection_kwargs()) as connection:
                assert connection.execute("SELECT 1").fetchone()[0] == 1
        # Killing one leaves the other untouched.
        servers[0].terminate()
        with psycopg.connect(**servers[1].connection_kwargs()) as connection:
            assert connection.execute("SELECT 1").fetchone()[0] == 1
    finally:
        for server in servers:
            server.stop()


@pytest.mark.postgres
def test_initialising_over_an_existing_cluster_refuses(running_server):
    with pytest.raises(SiloError, match="already exists"):
        running_server.create()


@pytest.mark.postgres
def test_stopping_a_server_that_already_died_is_quiet(running_server, monkeypatch):
    # THE race this found, with the interleaving FORCED rather than
    # hoped for. terminate() sends SIGQUIT and the postmaster removes
    # its own pid file while shutting down, so a stop() arriving in
    # between passes its own is_reachable() check and then fails inside
    # pg_ctl with "PID file does not exist".
    #
    # Written first as terminate-then-stop, which passed against a
    # stop() that trusted pg_ctl -- by the time stop() ran, the pid file
    # was already gone and the early return fired. That is a test hoping
    # for an interleaving, which is no test at all. Forcing it instead:
    # is_reachable() answers True on the pre-check and False afterwards,
    # which is exactly the window.
    running_server.terminate()
    answers = iter([True, False, False])
    monkeypatch.setattr(type(running_server), "is_reachable",
                        lambda self: next(answers))
    running_server.stop()


@pytest.mark.postgres
def test_stopping_twice_is_quiet(running_server):
    running_server.stop()
    running_server.stop()
    assert not running_server.is_reachable()


def test_a_process_that_cannot_be_signalled_counts_as_running():
    # kill(pid, 0) raises PermissionError precisely when the process
    # EXISTS and is not ours, so reading that as "dead" gets the
    # question backwards. Measured before the fix: is_alive(1) returned
    # False for PID 1.
    #
    # It mattered because await_death is the only caller: a process
    # that could not be signalled read as already dead, so terminate()
    # would return claiming a silo was down while it was up.
    import os

    from simulator.silos.process import is_alive

    try:
        os.kill(1, 0)
    except PermissionError:
        pass
    except OSError:
        pytest.skip("PID 1 is not there to ask about")
    else:
        pytest.skip("running as root, so PID 1 can be signalled")

    assert is_alive(1) is True


def test_liveness_is_asked_in_exactly_one_place():
    # PostgresSilo.is_reachable had its own copy of this check, which
    # got PermissionError right while the shared one got it wrong --
    # two liveness checks disagreeing about the same case, which is
    # worse than either alone.
    import inspect

    from simulator.silos.process import is_alive

    source = inspect.getsource(PostgresSilo.is_reachable)
    assert "is_alive" in source
    assert "os.kill" not in source
    assert "PermissionError" in inspect.getsource(is_alive)
