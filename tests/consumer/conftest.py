"""A world started the way anybody else would start one.

EVERY TEST UNDER tests/consumer/ IMPORTS NOTHING FROM `simulator`.
That is the point, and it is checked mechanically by
test_outside_in.py rather than left to discipline. The suite that
already exists proves the simulator writes what it meant to; it proves
nothing about whether an independent client, with its own driver and
no access to the simulator's objects, can read it back.

So the world here is started as a SUBPROCESS, through the same command
line a person would type, and the only thing that crosses from it to
the tests is connections.json -- the file a consumer is meant to
configure itself from. If that file is not enough, that is the finding.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parent.parent.parent
PACK = REPOSITORY / "packs" / "probe.yaml"

#: Long enough for two engines to initialise and a few simulated days
#: to run on a slow machine.
STARTUP_TIMEOUT_SECONDS = 180
SIMULATED_DAYS = 3


@pytest.fixture(scope="session")
def world_directory(tmp_path_factory):
    """Start a world, wait for it to be reachable, and leave it up."""
    _require_engines()
    directory = tmp_path_factory.mktemp("probe")
    log = (directory / "run.log").open("wb")

    process = subprocess.Popen(
        [sys.executable, "-m", "simulator", "run", str(PACK),
         "--dir", str(directory), "--days", str(SIMULATED_DAYS),
         "--seed", "11", "--console"],
        cwd=str(REPOSITORY),
        stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
        env={**os.environ, "PATH": _engine_path()},
    )
    try:
        _await_world(process, directory, log.name)
        yield directory
    finally:
        # `quit` rather than a signal, so the world is torn down the way
        # the console does it: a leaked cluster holds its port.
        try:
            process.stdin.write(b"quit\n")
            process.stdin.flush()
            process.stdin.close()
            process.wait(timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=30)
        log.close()


def _await_world(process, directory, log_path):
    """Wait until the world has published its connections AND is seeded.

    connections.json appears BEFORE the simulation starts, which is
    deliberate -- a consumer can connect during the backfill. These
    tests want the finished article, so they also wait for the console
    prompt, which only appears once the days have been simulated.
    """
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    connections = directory / "connections.json"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"the world exited during startup:\n{Path(log_path).read_text()}")
        if connections.exists() and "World is up" in Path(log_path).read_text():
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"the world was not up within {STARTUP_TIMEOUT_SECONDS}s:\n"
        f"{Path(log_path).read_text()}")


def _engine_path() -> str:
    """PATH with the PostgreSQL binaries on it, if they are not already.

    Debian and Ubuntu install to /usr/lib/postgresql/<major>/bin and
    leave it off PATH so several majors can coexist.
    """
    existing = os.environ.get("PATH", "")
    for candidate in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        if str(candidate) not in existing:
            return f"{candidate}{os.pathsep}{existing}"
    return existing


def _require_engines() -> None:
    import shutil

    missing = [name for name, found in (
        ("initdb", shutil.which("initdb") or list(Path("/usr/lib/postgresql").glob("*/bin/initdb"))),
        ("mariadbd", shutil.which("mariadbd") or Path("/usr/sbin/mariadbd").exists()),
    ) if not found]
    if missing:
        pytest.skip(f"needs {missing} to start a world")


@pytest.fixture(scope="session")
def connections(world_directory):
    """The only thing that crosses from the world to these tests."""
    return json.loads((world_directory / "connections.json").read_text())["silos"]


@pytest.fixture(params=["ops", "shop"])
def database(request, connections):
    """Each relational silo in turn.

    Parameterised so every assertion below runs against PostgreSQL AND
    MariaDB. A difference between them is not a nuisance -- it is
    exactly what a consumer has to be told about.
    """
    return request.param, connections[request.param]
