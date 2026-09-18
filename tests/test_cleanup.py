"""Picking up after a simulator that did not get to tidy up.

A world stopped properly leaves nothing running -- measured, the ports
are bindable the instant stop() returns and the directory is removable
with nothing referencing it. This is for the other case: the process
was killed, and the servers it started carried on serving with nothing
left that knew they existed.
"""

import pathlib
import subprocess
import sys
import time

import pytest

from simulator.cleanup import ENGINES, Stray, _is_engine, clean, strays, terminate

# -- what it will and will not touch ----------------------------------

def test_a_shell_that_mentions_the_path_is_not_a_server(tmp_path):
    # THE near-miss, kept as a test. A first version matched on the path
    # alone and signalled the shell running `simulator clean --dir
    # /tmp/crash`, because that command line mentions /tmp/crash too.
    # So does the grep looking for the cluster, and so would any editor
    # with a file from the world open.
    assert _is_engine("/usr/lib/postgresql/16/bin/postgres -D /tmp/w/cluster") is True
    assert _is_engine("/usr/sbin/mariadbd --datadir=/tmp/w/data") is True
    for innocent in (
        "/bin/sh -c python3 -m simulator clean --dir /tmp/w",
        "grep postgres /tmp/w/dispatch/postgres.log",
        "vim /tmp/w/connections.json",
        "su claude -s /bin/bash -c cd /tmp/w && postgres",
    ):
        assert _is_engine(innocent) is False, innocent


def test_the_engine_list_is_the_whole_guard(tmp_path):
    # If this list quietly emptied, nothing would ever be cleaned up
    # and the command would report success every time.
    assert set(ENGINES) >= {"postgres", "mariadbd", "mysqld"}


def test_nothing_is_found_for_a_directory_with_nothing_running(tmp_path):
    assert strays(tmp_path) == []
    result = clean(tmp_path)
    assert result == {"found": [], "stopped": [], "stubborn": [], "removed": False}


def test_a_process_naming_the_directory_is_found_only_if_it_is_a_server(tmp_path):
    # A real process, named so that it mentions the path, and running
    # something that is not a server.
    #
    # THE PRECONDITION IS ASSERTED, and that is not decoration. A first
    # version silently stopped mentioning the path in a way `ps` could
    # see -- so the test passed, and so did the control aimed at it,
    # which proved nothing. If the premise breaks again this fails
    # loudly instead of going quiet.
    marker = tmp_path / "w"
    marker.mkdir()
    process = subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep(30)  # {marker}"])
    try:
        time.sleep(0.8)
        command = (pathlib.Path("/proc") / str(process.pid) / "cmdline").read_bytes()
        assert str(marker) in command.replace(b"\0", b" ").decode(), (
            "the probe process does not name the directory, so this test would "
            "pass whatever the code did")

        assert strays(marker) == [], "a sleeping python was mistaken for a server"
    finally:
        process.kill()
        process.wait(timeout=10)


# -- stopping one ------------------------------------------------------

def test_a_stray_that_is_already_gone_counts_as_stopped():
    # The pid file outlives the process, so this is the ordinary case
    # rather than an edge one.
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=10)
    assert terminate(Stray(pid=process.pid, command="postgres -D /tmp/x")) is True


def test_a_stray_is_asked_to_stop_rather_than_killed(tmp_path):
    # SIGTERM, not SIGKILL. A world may be resumed, and a cluster killed
    # mid-write may not open again.
    script = "import signal, sys, time\n" \
             "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n" \
             "time.sleep(30)\n"
    process = subprocess.Popen([sys.executable, "-c", script])
    try:
        time.sleep(0.5)
        # Reaped in the background, because terminate() polls with
        # os.kill(pid, 0) and an UNREAPED child is a zombie that still
        # answers. Real strays are not our children, so init reaps them
        # and the case does not arise -- but a test that makes them our
        # children has to.
        import threading
        reaper = threading.Thread(target=process.wait, daemon=True)
        reaper.start()
        assert terminate(Stray(pid=process.pid, command="postgres"), timeout=10) is True
        reaper.join(timeout=10)
        code = process.returncode
        # Not -9. A process that handled SIGTERM and left on its own
        # terms exits normally or reports -15; only SIGKILL gives -9,
        # and that is what must not happen to a cluster mid-write.
        assert code != -9, "it was killed rather than asked"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_one_that_will_not_stop_is_reported_rather_than_forced(tmp_path):
    script = "import signal, time\n" \
             "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" \
             "time.sleep(30)\n"
    process = subprocess.Popen([sys.executable, "-c", script])
    try:
        time.sleep(0.5)
        assert terminate(Stray(pid=process.pid, command="postgres"), timeout=1.0) is False
        assert process.poll() is None, "it was forced after all"
    finally:
        process.kill()
        process.wait(timeout=10)


# -- and the directory -------------------------------------------------

def test_the_directory_survives_unless_asked_for(tmp_path):
    # It is the record of what happened, and resume reads it. Deleting
    # it as a side effect of tidying up processes would throw away a
    # simulated year to reclaim a port.
    (tmp_path / "connections.json").write_text("{}")
    clean(tmp_path)
    assert (tmp_path / "connections.json").exists()


def test_remove_deletes_it_when_nothing_is_using_it(tmp_path):
    world = tmp_path / "var"
    world.mkdir()
    (world / "connections.json").write_text("{}")
    result = clean(world, remove=True)
    assert result["removed"] is True
    assert not world.exists()


@pytest.mark.postgres
def test_a_world_stopped_properly_leaves_nothing_running(tmp_path, postgres_binaries):
    # The baseline the whole module is measured against: a clean stop
    # already leaves nothing behind, and this is only for crashes.
    import socket

    from worlds import running_world

    from tests.test_cleanup import SMALL

    with running_world(tmp_path, SMALL, "tidy", seed=1, days=1) as world:
        port = world.silo("ops").port
        directory = tmp_path / "var"
    assert strays(directory) == []
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


SMALL = """
pack: tidy
silos: {ops: {kind: postgresql, database: ops}}
schemas:
  ops:
    tables:
      notes:
        columns:
          note_id: {type: text, length: 64, primary_key: true, nullable: false}
seed:
  - table: ops.notes
    count: 2
    columns:
      note_id: {generator: id, prefix: n}
"""
