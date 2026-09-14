"""
sqlite.py  (a silo that is a file, because that is what it really is)

THE AWKWARD ONE, AND THE HONEST ONE. A SQLite silo has no port, no
process, and nothing to start. Every method below that does nothing is
doing nothing for a real reason, not as a placeholder.

WHY IT BELONGS HERE AT ALL. Small businesses run a great deal of
SQLite without ever calling it that: it is embedded in desktop
point-of-sale software, in line-of-business applications, in the
accounting package on the back-office PC. The business does not have
"a SQLite database" -- it has an application, and the application's
file is what a data platform ends up pointing at. Foundry lists SQLite
among its relational connectors for exactly this reason.

It was dropped from an earlier version of this design on the grounds
that it cannot listen on a port. That is true and it was the wrong
conclusion: the fix is not to exclude the technology, it is to stop
assuming every silo is reached the same way. A consumer of a SQLite
silo really does open a file, and `connection()` says so.

WAL IS SET AT CREATION, and verified rather than assumed: with the
database in WAL mode, a reader is not blocked while a writer holds an
open transaction, and sees the commit immediately afterwards. Journal
mode is a persistent property of the file, so a consumer picks it up
with no configuration. In the default rollback-journal mode a live
consumer would intermittently block on the simulator's own writes, and
that contention would be an artefact of the tool rather than a
property of the business being simulated.
"""

import sqlite3
from pathlib import Path
from typing import ClassVar

from simulator.silo import ConnectionDescriptor, Silo, SiloError

#: The first sixteen bytes of every SQLite database file. Checking for
#: it is the only way to tell a real database from a zero-byte file,
#: because SQLite treats a zero-length file as a VALID EMPTY DATABASE
#: -- measured: opening one and querying sqlite_master succeeds and
#: returns 0. A silo this module created always carries the header,
#: since setting the journal mode writes it.
SQLITE_MAGIC = b"SQLite format 3\x00"

#: What a terminated silo's file is renamed to. Recoverable on purpose:
#: a real outage here is usually a backup script or a sync client
#: moving the file, not a deletion, and a test that wants the file back
#: can move it back.
TERMINATED_SUFFIX = ".gone"


class SqliteSilo(Silo):
    """One silo, backed by a single SQLite file."""

    kind: ClassVar[str] = "sqlite"
    #: No port, ever. This is the flag's whole reason for existing.
    requires_port: ClassVar[bool] = False

    def __init__(self, name: str, data_dir: Path, filename: str | None = None) -> None:
        super().__init__(name, data_dir)
        #: Named after the silo by default, so a directory of them
        #: reads as a list of systems rather than of files.
        self.filename = filename or f"{name}.db"

    @property
    def path(self) -> Path:
        return self.data_dir / self.filename

    # -- lifecycle ---------------------------------------------------

    def create(self) -> None:
        if self.path.exists():
            raise SiloError(
                f"{self.name}: {self.path} already exists; remove the world's "
                f"directory to rebuild it"
            )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # The one place creation is the intent, so a bare path is
        # correct here -- everywhere else opens with mode=rw so that a
        # missing file fails instead of silently reappearing.
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.commit()
        finally:
            connection.close()

    def start(self) -> None:
        """Nothing to start. A file is openable or it is not.

        Not a stub: this is what starting a file-based silo means, and
        the Silo contract allows it precisely so that this can be
        honest rather than fake a daemon.
        """
        return None

    def stop(self) -> None:
        """Nothing to stop. See start()."""
        return None

    def is_reachable(self) -> bool:
        """Whether the file exists and opens as a database.

        Existence alone is not enough, and the difference is not
        academic: a zero-byte file is what gets left behind when
        something calls sqlite3.connect() on a path that is not there,
        since a bare connect CREATES rather than fails. A silo that has
        been terminated and then touched by a careless reader would
        pass an exists() check forever after.
        """
        if not self.path.exists():
            return False
        # The header check comes FIRST and is not redundant with the
        # query below. A zero-byte file is a valid empty database as far
        # as SQLite is concerned -- querying sqlite_master on one
        # succeeds -- so opening it proves nothing. The header is what
        # distinguishes a database from a file something else created.
        try:
            with self.path.open("rb") as handle:
                if handle.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
                    return False
        except OSError:
            return False
        try:
            connection = sqlite3.connect(f"file:{self.path}?mode=rw", uri=True)
        except sqlite3.Error:
            return False
        try:
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return True
        except sqlite3.DatabaseError:
            return False
        finally:
            connection.close()

    def connection(self, database: str | None = None) -> ConnectionDescriptor:
        _refuse_database(self.name, self.kind, database)
        """A path, not a host and port. That is the point."""
        return ConnectionDescriptor(kind=self.kind, details={"path": str(self.path)})

    def terminate(self) -> None:
        """Move the file aside, so the silo abruptly stops answering.

        The file-based equivalent of killing a server, and a real
        failure rather than an invented one: a backup script, a cloud
        sync client or somebody tidying a shared drive is how a desktop
        application's database actually goes missing.

        Renamed rather than deleted so the condition is recoverable and
        so a test can put it back.
        """
        if self.path.exists():
            self.path.replace(self.path.with_suffix(self.path.suffix + TERMINATED_SUFFIX))

    def connect(self) -> sqlite3.Connection:
        """An open connection that refuses to create the file.

        `mode=rw` rather than a bare path, because sqlite3.connect()
        CREATES a missing database instead of failing -- which turns
        "the silo is gone" into "here is an empty database" silently,
        and defeats any check based on the file's existence.
        """
        try:
            connection = sqlite3.connect(f"file:{self.path}?mode=rw", uri=True, timeout=10.0)
        except sqlite3.OperationalError as error:
            raise SiloError(f"{self.name}: cannot open {self.path}: {error}") from error
        connection.row_factory = sqlite3.Row
        return connection


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): SQLite was dropped from an earlier version of
# this design because it cannot listen on a port, and brought back once the
# conclusion was recognised as the wrong one. The requirement was that a
# consumer reach the silos directly; it was never that every silo be a server.
# Small businesses genuinely run SQLite inside desktop applications, and
# Foundry lists it as a relational connector.
#
# RESOLVED: is_reachable() checks the sixteen-byte file header, not just that
# the file opens. Opening is not enough, and this was found by a test that
# failed: SQLite treats a ZERO-LENGTH file as a valid empty database, so
# querying sqlite_master on the artefact a careless sqlite3.connect() leaves
# behind succeeds and returns 0. Measured directly -- zero bytes, query
# returns (0,) -- while a database this module created is 4096 bytes and
# begins "SQLite format 3\x00". Existence alone was the first version and was
# worse still; the same failure mode was measured defeating a health check in
# another system while researching this.
#
# RESOLVED: terminate() renames rather than deletes. A deletion is not the
# realistic failure -- a backup script or sync client moving the file is -- and
# a recoverable condition is more useful to test against.
#
# DEFERRED (known, intentional, not yet built): no support for a silo made of
# SEVERAL SQLite files, which is how some desktop applications actually store
# things (a company file plus attachments plus an index). One file covers every
# case a pack has needed; the extension is a list of paths in the descriptor.


def _refuse_database(name: str, kind: str, database: str | None) -> None:
    """A sqlite silo holds no databases.

    Refusing rather than ignoring: a pack declaring one would otherwise
    have written something with no effect, and the author would have no
    way to find out.
    """
    if database is not None:
        raise SiloError(
            f"silo {name!r} is a {kind!r} silo and holds no database called {database!r}"
        )
