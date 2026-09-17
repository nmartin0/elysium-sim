"""
mariadb.py  (a silo backed by its own MariaDB instance)

The most common DATABASE A small business actually runs. Shared LAMP
hosting gives you MySQL or MariaDB and nothing else, so a web store --
WooCommerce, PrestaShop, OpenCart -- is almost always sitting on one.
Foundry lists MariaDB and MySQL as separate relational connectors;
they speak the same wire protocol and the same client works for both.

Why this is not a subclass of PostgresSilo, despite the shape being
close. The two differ in every detail that matters: initialisation is
`mariadb-install-db` rather than `initdb`, there is no `pg_ctl`
equivalent that daemonises and waits, shutdown goes through a client
command rather than a control program, and readiness has to be
established by connecting rather than by the start command blocking.
Sharing a base class would mean a template method per difference and a
parent that is really two implementations interleaved. They share the
`Silo` contract, which is the thing they genuinely have in common.

No daemonise-and-wait, which is the real complication here. `pg_ctl
-w` blocks until the server is accepting connections. `mariadbd` has
no such mode -- it runs in the foreground until killed -- so this
spawns it and polls until it answers. Polling is not elegant and it is
what the absence of a control program leaves.

And it polls with a protocol ping, not A TCP connect. That distinction
was a real bug, found by a test: a bare TCP connect proves only that
something is listening on that port. When the port was already taken,
the connect succeeded against the squatter and startup was reported as
successful while the server had actually exited. `mariadb-admin ping`
speaks the real protocol, so it can only succeed against a real
server. The contrived case is a test; the case it stands for -- a
leftover instance from an earlier run, or an unrelated service on the
pinned port -- is not.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from simulator.silo import ConnectionDescriptor, Silo, SiloError
from simulator.silos.connection import SharedConnection, server_descriptor
from simulator.silos.logs import rotate
from simulator.silos.process import await_death, recorded_pid
from simulator.silos.reader import READER

#: MariaDB's own administrative account, created by mariadb-install-db.
#: Unlike PostgreSQL, the superuser name is not ours to choose at
#: initialisation time without extra work, so this follows the tool.
DEFAULT_SUPERUSER = "root"

#: Created by mariadb-install-db and always present, so administrative
#: work has somewhere to connect before any business database exists.
MAINTENANCE_DATABASE = "mysql"

#: Where the daemon lives when a normal user's path does not include
#: it. On many systems /usr/sbin is only on root's path, so `command -v
#: mariadbd` comes back empty for the very user this must run as.
_SBIN_DIRECTORIES = ("/usr/sbin", "/usr/local/sbin", "/usr/libexec/mysqld")

READY_TIMEOUT_SECONDS = 60
#: Polling interval while waiting for the port to answer. Short enough
#: that startup feels instant, long enough not to spin.
_POLL_SECONDS = 0.2


class MariaDbUnavailable(Exception):
    """No usable MariaDB or MySQL installation was found."""


@dataclass(frozen=True)
class MariaDbBinaries:
    """Where the three tools this needs actually live."""

    install_db: Path
    daemon: Path
    admin: Path

    @classmethod
    def discover(cls) -> "MariaDbBinaries":
        found: dict[str, Path] = {}
        # MySQL and MariaDB ship the same tools under both names in
        # most distributions; either is acceptable and the first hit
        # wins, because a machine with both installed has one of them
        # shadowing the other on path anyway.
        wanted = {
            "install_db": ("mariadb-install-db", "mysql_install_db"),
            "daemon": ("mariadbd", "mysqld"),
            "admin": ("mariadb-admin", "mysqladmin"),
        }
        for key, names in wanted.items():
            for name in names:
                located = shutil.which(name) or _search_sbin(name)
                if located:
                    found[key] = Path(located)
                    break
        missing = sorted(set(wanted) - set(found))
        if missing:
            raise MariaDbUnavailable(
                f"could not find {missing} on PATH or in {list(_SBIN_DIRECTORIES)}. "
                f"Install mariadb-server (or mysql-server), or put its bin and sbin "
                f"directories on PATH."
            )
        return cls(install_db=found["install_db"], daemon=found["daemon"], admin=found["admin"])


def _search_sbin(name: str) -> str | None:
    for directory in _SBIN_DIRECTORIES:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


class MariaDbSilo(Silo):
    """One silo, backed by its own MariaDB instance."""

    kind: ClassVar[str] = "mariadb"
    requires_port: ClassVar[bool] = True

    def __init__(self, name: str, data_dir: Path, port: int,
                 binaries: MariaDbBinaries | None = None,
                 superuser: str = DEFAULT_SUPERUSER) -> None:
        super().__init__(name, data_dir)
        self.port = port
        self.binaries = binaries or MariaDbBinaries.discover()
        self.superuser = superuser
        #: Holding one connection open across a block of work, and
        #: deciding when an open one will serve. Shared with the other
        #: SQL silo because it was 100% identical between them; see
        #: connection.py for why that is a collaborator rather than a
        #: base class.
        self._connections = SharedConnection(open=self._open)
        self._process: subprocess.Popen | None = None

    @property
    def cluster_dir(self) -> Path:
        return self.data_dir / "data"

    @property
    def socket_path(self) -> Path:
        """A short path, outside the world, named for it.

        A Unix socket path has a hard 107-byte limit, and a world a few
        directories deep exceeds it -- measured: MariaDB refuses to
        start with "The socket file path is too long (> 107)". So the
        socket cannot live inside the world, however much tidier that
        would be.

        PostgreSQL solves this by having no socket at all, since every
        connection here is TCP. MariaDB requires one, so instead the
        path is short and derived from a hash of the world's own
        directory: short enough to fit, and unique enough that two
        worlds run by the same user cannot collide.
        """
        digest = hashlib.blake2b(str(self.data_dir.resolve()).encode(),
                                 digest_size=6).hexdigest()
        return Path(tempfile.gettempdir()) / f"simsock-{digest}.sock"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "mariadb.log"

    @property
    def query_log_path(self) -> Path:
        """Separate from the server log, because they answer different
        questions: one is why the server is unhappy, the other is what
        was asked of it."""
        return self.data_dir / "queries.log"

    @property
    def pid_path(self) -> Path:
        return self.data_dir / "mariadb.pid"

    # -- lifecycle ---------------------------------------------------

    def create(self) -> None:
        if self.cluster_dir.exists():
            raise SiloError(
                f"{self.name}: {self.cluster_dir} already exists; remove the "
                f"world's directory to rebuild it"
            )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                str(self.binaries.install_db),
                f"--datadir={self.cluster_dir}",
                # Password-less root over the loopback socket. Same
                # reasoning as the PostgreSQL silo: the data is
                # fictional, the instance is not reachable off the
                # machine, and a password would be ceremony every
                # consumer then carries in its configuration.
                "--auth-root-authentication-method=normal",
                "--skip-test-db",
            ],
            capture_output=True, text=True, timeout=READY_TIMEOUT_SECONDS * 2,
        )
        if result.returncode != 0:
            raise SiloError(f"{self.name}: mariadb-install-db failed\n{result.stderr.strip()}")

    def start(self) -> None:
        # Before the server opens it. A log rotated while something is
        # appending to it keeps being appended to under its old name,
        # so the new file stays empty and the old one keeps growing --
        # which is the failure this is meant to prevent.
        rotate(self.query_log_path)

        if not self.cluster_dir.exists():
            raise SiloError(f"{self.name}: nothing at {self.cluster_dir}; create it first")
        if self.is_reachable():
            return
        log = self.log_path.open("ab")
        self._process = subprocess.Popen(
            [
                str(self.binaries.daemon),
                f"--datadir={self.cluster_dir}",
                f"--port={self.port}",
                f"--socket={self.socket_path}",
                f"--pid-file={self.pid_path}",
                # Loopback only. A simulated business answering on a LAN
                # interface would be a genuinely bad thing to leave running.
                "--bind-address=127.0.0.1",
                # The general query log: every statement, with the
                # account and connection that issued it. See the same
                # note in postgres.py -- this records attempts, not
                # only successes.
                "--general-log=1",
                f"--general-log-file={self.query_log_path}",
            ],
            stdout=log, stderr=log,
        )
        self._await_readiness()

    def _await_readiness(self) -> None:
        """Poll the port until it answers, or give up saying why.

        mariadbd has no equivalent of `pg_ctl -w`: it runs in the
        foreground and never reports readiness, so the only honest
        signal is the port accepting a connection.
        """
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                # It exited rather than started. The log has the reason,
                # and making someone go find it is a bad experience at
                # exactly the moment they are already confused.
                raise SiloError(f"{self.name}: mariadbd exited during startup\n{self._log_tail()}")
            if self._ping():
                return
            time.sleep(_POLL_SECONDS)
        raise SiloError(
            f"{self.name}: mariadbd did not accept connections on port {self.port} "
            f"within {READY_TIMEOUT_SECONDS}s\n{self._log_tail()}"
        )

    def _ping(self) -> bool:
        """Whether a real MariaDB answers on our port.

        A protocol-level check, deliberately. See the module note: a
        bare TCP connect cannot tell our server from anything else that
        happens to hold the port, and reported a successful start
        against a squatter while the server had exited.
        """
        try:
            result = subprocess.run(
                [
                    str(self.binaries.admin),
                    "--protocol=tcp", "--host=127.0.0.1",
                    f"--port={self.port}", f"--user={self.superuser}",
                    "--connect-timeout=2",
                    "ping",
                ],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def stop(self) -> None:
        """Stop, if running. Quiet when it is not.

        Promises the outcome rather than the mechanism, for the same
        reason the PostgreSQL silo does: a server that exits on its own
        between the check and the shutdown command must not turn a
        successful teardown into an error.
        """
        if not self.is_reachable():
            self._reap()
            return
        result = subprocess.run(
            [
                str(self.binaries.admin),
                "--protocol=tcp", "--host=127.0.0.1",
                f"--port={self.port}", f"--user={self.superuser}",
                "shutdown",
            ],
            capture_output=True, text=True, timeout=READY_TIMEOUT_SECONDS,
        )
        self._reap()
        if result.returncode != 0 and self.is_reachable():
            raise SiloError(f"{self.name}: shutdown failed\n{result.stderr.strip()}")

    def _reap(self) -> None:
        """Wait for our own child, so it does not linger as a zombie."""
        if self._process is not None:
            try:
                self._process.wait(timeout=READY_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=10)
            self._process = None

    def is_reachable(self) -> bool:
        return self._ping()

    def session(self, database: str):
        """Hold one connection open for a block. See connection.py."""
        return self._connections.session(database)

    def connect(self, database: str = MAINTENANCE_DATABASE, *, autocommit: bool = False):
        """A connection for one piece of work. See connection.py."""
        return self._connections.use(database, autocommit=autocommit)

    def connection(self, database: str | None = None) -> ConnectionDescriptor:
        """How a consumer reaches this silo. See connection.py.

        (The previous version put the `database or MAINTENANCE_DATABASE`
        line above its docstring, which meant the method had no
        docstring at all -- a string expression preceded by a statement
        is just a string. Silent, and invisible until this was
        rewritten.)
        """
        # The READER, not root. What this advertised before was
        # `GRANT ALL PRIVILEGES ON *.* WITH GRANT OPTION`.
        return server_descriptor(self.kind, self.port,
                                 database or MAINTENANCE_DATABASE, READER)

    @contextmanager
    def _open(self, database: str, *, autocommit: bool):
        """A brand new connection, closed on the way out."""
        import pymysql

        connection = pymysql.connect(
            host="127.0.0.1", port=self.port, database=database, user=self.superuser,
            autocommit=autocommit, charset="utf8mb4",
        )
        try:
            yield connection
        finally:
            connection.close()

    def connection_kwargs(self, database: str = MAINTENANCE_DATABASE) -> dict[str, object]:
        """Keyword arguments for a DB-API connect(). See PostgresSilo."""
        return {
            "host": "127.0.0.1",
            "port": self.port,
            "database": database,
            "user": self.superuser,
        }

    def driver_errors(self) -> tuple[type[Exception], ...]:
        """Everything this driver raises for a query that cannot run.

        Both drivers root their exceptions at a single base, which is
        PEP 249's own arrangement, so one entry covers the lot.
        """
        import pymysql

        return (pymysql.Error,)

    def terminate(self) -> None:
        """Kill the server without a clean shutdown. See Silo.terminate."""
        if self._process is not None and self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=10)
            self._process = None
            return
        pid = recorded_pid(self.pid_path)
        if pid is None:
            return
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            return
        await_death(pid)

    def _log_tail(self) -> str:
        if not self.log_path.exists():
            return "(no log)"
        lines = self.log_path.read_text(errors="replace").splitlines()[-12:]
        return f"--- {self.log_path} ---\n" + "\n".join(lines)


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): this is not a subclass of PostgresSilo. The two
# differ in initialisation, in whether a control program exists, in how
# shutdown is issued, and in how readiness is established -- a shared parent
# would be two implementations interleaved behind template methods. What they
# genuinely share is the Silo contract.
#
# RESOLVED: readiness polls with `mariadb-admin ping`, not a TCP connect. The
# first version used a bare socket connect, and a test caught it: with the port
# already held by something else, the connect succeeded and start() reported
# success while mariadbd had exited. A protocol ping can only succeed against a
# real server. The cost is a subprocess per check, which is acceptable because
# a start converges in a handful of polls and nothing else calls it in a loop.
#
# RESOLVED: binaries are searched in /usr/sbin as well as on path. On many
# systems /usr/sbin is only on root's path, so `command -v mariadbd` returns
# nothing for exactly the unprivileged user this has to run as.
#
# RESOLVED: the socket lives outside the world, at a short hashed path. It was
# inside, to avoid /tmp collisions, until a 107-byte limit on sun_path made
# that impossible for any world more than a few directories deep -- measured,
# with the server refusing to start. The hash keeps the collision property the
# original placement was chosen for.
#
# DEFERRED (known, intentional, not yet built): no MySQL-specific silo, even
# though Foundry lists MySQL and MariaDB separately. They speak the same
# protocol, the same client works for both, and discover() already accepts
# either set of binaries. A separate kind would be worth it only if a pack
# needed to declare which fork it is emulating, and none does yet.
#
# DEFERRED: the superuser is whatever mariadb-install-db creates (root) rather
# than a name of our choosing, unlike the PostgreSQL silo where initdb -U
# accepts one. Changing it means a CREATE user after initialisation, which is
# real work for a cosmetic gain.
