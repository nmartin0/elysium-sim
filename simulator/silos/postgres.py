"""
postgres.py  (a silo backed by its own PostgreSQL instance)

THE SIMULATOR STARTS ITS OWN SERVERS. It never uses a system service,
never needs root, never touches a cluster it did not create. Each
simulated silo gets its own instance, its own data directory, and its
own port -- which is what a silo actually is. Separate systems in a
real organization have separate failure domains and separate
credentials, and an instance that can be individually killed turns
"the database went away" from something to be mocked into something
that simply happens.

TWO FACTS ABOUT REAL MACHINES that this file exists to absorb, both
verified directly rather than assumed:

  - ON DEBIAN AND UBUNTU THE BINARIES ARE NOT ON PATH. The packages
    install to /usr/lib/postgresql/<major>/bin and deliberately leave
    it off PATH so several major versions can coexist. `which initdb`
    comes back empty on a machine with a perfectly good PostgreSQL 16
    on it. Discovery therefore checks PATH first and then those
    directories, newest major version first.
  - INITDB REFUSES TO RUN AS ROOT, with "cannot be run as root" and a
    hint to su to an unprivileged user. That refusal is correct and
    this file does not work around it -- it checks first and says the
    same thing earlier, because a caller who sees the message before
    anything happens is better off than one who sees it in the middle
    of a subprocess trace.

WHY pg_ctl RATHER THAN RUNNING `postgres` DIRECTLY. pg_ctl's `-w`
waits until the server is genuinely accepting connections rather than
merely spawned, which is the difference between a start() that means
something and one that returns before the port answers. It also owns
the pid file, which is what makes a stale one detectable.
"""

import os
import shutil
import signal
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from simulator.silo import ConnectionDescriptor, Silo, SiloError

#: Where Debian and Ubuntu put them. Ordered newest-first at discovery
#: so a machine with several majors installed gets the newest, which is
#: also what `pg_ctl` in a fresh data directory expects.
_PACKAGED_BIN_GLOB = "/usr/lib/postgresql/*/bin"

#: The database initdb always creates, regardless of the superuser
#: name. `initdb -U sim` creates the ROLE `sim`; it does not create a
#: database called `sim`, and assuming otherwise is a mistake worth
#: naming here because it looks exactly like a working configuration
#: until something tries to connect. Administrative work -- CREATE
#: DATABASE, and asking whether one exists -- goes through this one.
MAINTENANCE_DATABASE = "postgres"

#: The role the simulator creates and owns. Not `postgres`: a superuser
#: named after the tool makes it obvious in `pg_stat_activity` which
#: connections belong to a simulated world.
DEFAULT_SUPERUSER = "sim"

#: How long to wait for a start or a stop before giving up. Generous --
#: a first start runs initdb's fsync-heavy setup, and a slow machine
#: under load is not a failure.
TIMEOUT_SECONDS = 60


class PostgresUnavailable(Exception):
    """No usable PostgreSQL installation was found."""


@dataclass(frozen=True)
class PostgresBinaries:
    """Where initdb and pg_ctl actually live on this machine."""

    initdb: Path
    pg_ctl: Path

    @classmethod
    def discover(cls) -> "PostgresBinaries":
        found = {}
        for name in ("initdb", "pg_ctl"):
            on_path = shutil.which(name)
            if on_path:
                found[name] = Path(on_path)
                continue
            # Newest major first. sorted() on the glob is lexical, which
            # orders "16" before "9.6" wrongly -- but PostgreSQL has not
            # shipped a 9.x since 2021 and the packaged directories are
            # all two-digit now, so lexical descending is correct in
            # practice and stated here so the limit is visible.
            candidates = sorted(Path("/").glob(_PACKAGED_BIN_GLOB.lstrip("/")), reverse=True)
            for directory in candidates:
                candidate = directory / name
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    found[name] = candidate
                    break
        missing = [name for name in ("initdb", "pg_ctl") if name not in found]
        if missing:
            raise PostgresUnavailable(
                f"could not find {' and '.join(missing)} on PATH or under "
                f"{_PACKAGED_BIN_GLOB}. Install PostgreSQL, or put its bin "
                f"directory on PATH."
            )
        return cls(initdb=found["initdb"], pg_ctl=found["pg_ctl"])


def refuse_if_root() -> None:
    """Stop early if running as root, matching PostgreSQL's own rule.

    initdb exits with "cannot be run as root" and a hint to su. This
    raises the same thing before any directory is created, so the
    caller is not left with a half-built world and a subprocess trace.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise SiloError(
            "PostgreSQL refuses to run as root, and so does this. Run the "
            "simulator as an ordinary user -- the databases it creates live "
            "under the data directory you give it and need no privileges."
        )


class PostgresSilo(Silo):
    """One silo, backed by its own PostgreSQL instance."""

    kind: ClassVar[str] = "postgresql"
    requires_port: ClassVar[bool] = True

    def __init__(self, name: str, data_dir: Path, port: int,
                 binaries: PostgresBinaries | None = None,
                 superuser: str = DEFAULT_SUPERUSER) -> None:
        super().__init__(name, data_dir)
        self.port = port
        #: Discovered lazily by default so a caller does not have to
        #: locate binaries it has no opinion about, but injectable so a
        #: test can hand in a known-bad pair without patching.
        self.binaries = binaries or PostgresBinaries.discover()
        self.superuser = superuser

    @property
    def cluster_dir(self) -> Path:
        return self.data_dir / "cluster"

    @property
    def socket_dir(self) -> Path:
        """Unix socket directory, kept inside the world.

        Not /tmp. Two worlds built by the same user would otherwise
        collide on socket filenames, and a stale socket in /tmp outlives
        everything that could explain it.
        """
        return self.data_dir / "socket"

    def connection(self, database: str = MAINTENANCE_DATABASE) -> ConnectionDescriptor:
        """How a consumer reaches this silo."""
        return ConnectionDescriptor(kind=self.kind, details={
            "host": "127.0.0.1",
            "port": self.port,
            "database": database,
            "user": self.superuser,
        })

    @contextmanager
    def connect(self, database: str = MAINTENANCE_DATABASE, *, autocommit: bool = False):
        """An open DB-API connection, closed on the way out.

        autocommit matters here in a way it does not on MariaDB:
        PostgreSQL refuses CREATE DATABASE inside a transaction block,
        so provisioning has to ask for it explicitly.

        Imported inside the method rather than at module scope so that
        the lifecycle half of this file -- discovery, start, stop --
        keeps working on a machine with no driver installed. Starting
        and stopping a server needs binaries, not psycopg.
        """
        import psycopg

        connection = psycopg.connect(
            host="127.0.0.1", port=self.port, dbname=database, user=self.superuser,
            autocommit=autocommit, connect_timeout=10,
        )
        try:
            yield connection
        finally:
            connection.close()

    def connection_kwargs(self, database: str = MAINTENANCE_DATABASE) -> dict[str, object]:
        """Keyword arguments for psycopg.connect().

        A dict rather than a DSN string, because every caller here
        passes it straight to psycopg and a string would mean building
        it only to have psycopg parse it apart again. Loopback is not a
        parameter: the server is started with
        listen_addresses=127.0.0.1 and connecting any other way could
        not work.
        """
        return {
            "host": "127.0.0.1",
            "port": self.port,
            "dbname": database,
            "user": self.superuser,
        }

    @property
    def log_path(self) -> Path:
        return self.data_dir / "postgres.log"

    @property
    def pid_path(self) -> Path:
        return self.cluster_dir / "postmaster.pid"

    # -- lifecycle ---------------------------------------------------

    def create(self) -> None:
        """Initialise the cluster. Refuses rather than replacing."""
        refuse_if_root()
        if self.cluster_dir.exists():
            raise SiloError(
                f"{self.name}: {self.cluster_dir} already exists; remove the "
                f"world's directory to rebuild it"
            )
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        self._run([
            str(self.binaries.initdb),
            "-D", str(self.cluster_dir),
            "-U", self.superuser,
            # Trust auth on a loopback-only socket. The simulated data is
            # fictional by construction and the instance is not reachable
            # off the machine; a password would be ceremony that every
            # consumer then has to carry in its configuration.
            "--auth=trust",
            "--encoding=UTF8",
        ], "initdb")

    def start(self) -> None:
        refuse_if_root()
        if not self.cluster_dir.exists():
            raise SiloError(f"{self.name}: no cluster at {self.cluster_dir}; initialise it first")
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        options = (
            f"-p {self.port} "
            f"-k {self.socket_dir} "
            # Loopback only. A simulated world that answered on a LAN
            # interface would be a genuinely bad thing to leave running.
            f"-c listen_addresses=127.0.0.1"
        )
        self._run([
            str(self.binaries.pg_ctl),
            "-D", str(self.cluster_dir),
            "-l", str(self.log_path),
            "-o", options,
            "-w", "-t", str(TIMEOUT_SECONDS),
            "start",
        ], "start", include_log_on_failure=True)

    def stop(self) -> None:
        """Stop, if running. Quiet when it is not.

        THE PRE-CHECK IS NOT ENOUGH, and pretending otherwise produced a
        real failure. Between is_reachable() returning True and pg_ctl
        actually running, the server can exit on its own -- which is
        exactly what happens after terminate(), because the postmaster
        removes its own pid file while shutting down. pg_ctl then fails
        with "PID file does not exist. Is server running?" and the
        teardown blows up on a server that had already stopped.

        So the failure is re-examined rather than trusted: this method
        promises the OUTCOME (the server is not running) and not the
        mechanism (pg_ctl succeeded). If the server is gone, the goal is
        met however it got there. Anything else still raises.
        """
        if not self.is_reachable():
            return
        try:
            self._run([
                str(self.binaries.pg_ctl),
                "-D", str(self.cluster_dir),
                # `fast` rather than `smart`: smart waits for clients to
                # disconnect, and a consumer holding an idle connection
                # would hang the shutdown indefinitely.
                "-m", "fast",
                "-w", "-t", str(TIMEOUT_SECONDS),
                "stop",
            ], "stop")
        except SiloError:
            if self.is_reachable():
                raise
            self.pid_path.unlink(missing_ok=True)

    def is_reachable(self) -> bool:
        """Whether the postmaster this silo recorded is still alive.

        Deliberately a process check rather than a connection attempt.
        Opening a connection to answer "is it up" costs a round trip on
        every call and, worse, would make stop() -- which asks this
        first -- fail differently depending on how busy the server is.
        """
        pid = self._recorded_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # The pid exists and belongs to someone else. Treating that
            # as "running" is the safe reading: it means something is
            # there, and the caller should not assume the port is free.
            return True
        return True

    # -- internals ---------------------------------------------------

    def _recorded_pid(self) -> int | None:
        if not self.pid_path.exists():
            return None
        try:
            return int(self.pid_path.read_text().splitlines()[0])
        except (ValueError, IndexError, OSError):
            return None

    def _run(self, command: list[str], what: str, *, include_log_on_failure: bool = False) -> None:
        result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS * 2)
        if result.returncode == 0:
            return
        detail = (result.stderr or result.stdout).strip()
        if include_log_on_failure and self.log_path.exists():
            # pg_ctl's own failure message is usually just "could not
            # start server"; the reason is in the server log, and making
            # someone go find it is a bad experience at exactly the
            # moment they are already confused.
            tail = "\n".join(self.log_path.read_text(errors="replace").splitlines()[-10:])
            detail = f"{detail}\n--- {self.log_path} ---\n{tail}"
        raise SiloError(f"{self.name}: {what} failed\n{detail}")

    def terminate(self) -> None:
        """Kill the server without a clean shutdown.

        NOT an error path -- a deliberate one. "The database went away"
        is a condition a consumer should be tested against, and with a
        real server it can simply be made to happen rather than mocked.
        The port stops answering and, unlike a deleted file, nothing can
        accidentally recreate it.
        """
        pid = self._recorded_pid()
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGQUIT)
        except (ProcessLookupError, PermissionError):
            return


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): binaries are discovered rather than required on
# PATH, because Debian and Ubuntu install to /usr/lib/postgresql/<major>/bin and
# deliberately leave it off PATH so several majors can coexist. Verified on the
# machine this was written on: `which initdb` empty, /usr/lib/postgresql/16/bin/
# initdb present and working. Requiring PATH would fail on the most common
# Linux setup there is.
#
# RESOLVED: refuse_if_root() duplicates a check initdb already performs. Worth
# it: initdb's version fires after the caller has committed to a world
# directory, from inside a subprocess trace. This one fires before anything is
# created and explains what to do instead.
#
# RESOLVED: `-m fast` on stop. `smart` waits for clients to disconnect, and a
# consumer holding an idle connection hangs the shutdown indefinitely --
# exactly the situation this is used in.
#
# RESOLVED: there was a _clear_stale_pid() here, removing a pid file left by a
# killed server on the belief that pg_ctl would otherwise refuse to start over
# it. A negative control showed the test passing without it, and checking
# directly explained why: pg_ctl already detects a stale pid file and proceeds.
# Verified by writing 4194305 into postmaster.pid and starting -- "server
# started", no complaint. The code was doing nothing, so it is gone; the test
# stays, now documenting the dependency assumption rather than our own code.
#
# DEFERRED (known, intentional, not yet built): no `restart`. Nothing needs it;
# stop-then-start is two calls and the composite would hide which half failed.
#
# DEFERRED: trust authentication on a loopback socket, with no password and no
# per-consumer role. That is right for a fictional world unreachable off the
# machine, and it is also the thing to revisit when the simulator is used to
# exercise a consumer's own credential handling -- a read-only role with column
# GRANTs is the obvious next step, and PostgreSQL supports exactly that, which
# is a large part of why this stopped being SQLite.
#
# DEFERRED: no cleanup of orphaned instances at process exit. If the simulator
# is killed, its servers keep running and the next start finds the port taken --
# reported by PortRegistry.verify_available(), which names the port, but a
# `simulator stop --all` reading ports.json would be kinder.
