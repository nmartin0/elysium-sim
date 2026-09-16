"""
silo.py  (what a silo is, once it stops meaning "a PostgreSQL database")

A silo is a system a business runs, presented the way that system is
really reached. That is the whole abstraction, and it is deliberately
wider than "a database on a port", because small businesses do not run
one kind of thing:

  - a web store on MySQL or MariaDB, because that is what shared LAMP
    hosting gives you;
  - a desktop point-of-sale or line-of-business application on SQLite,
    embedded in the application, reached by opening a file;
  - a modern self-hosted system on PostgreSQL;
  - and, more often than any of those, a folder of CSV and Excel that
    somebody's bank, supplier or payroll provider drops files into.

Every one of those appears on Palantir Foundry's own connector list --
MariaDB, MySQL, PostgreSQL and SQLite are all named relational
sources, and plain directories, SMB shares and SFTP are all named file
sources. So this is not a set invented here; it is the intersection of
what small businesses actually run with what a platform of this kind
actually reads.

The awkward member is the honest one. A SQLite silo has no port, no
process, and nothing to start. Forcing it to pretend otherwise -- a
`start()` that fakes a daemon, a port nobody listens on -- would be
modelling a fiction for the sake of a uniform interface. So the
contract below allows a silo to have no port and a no-op lifecycle,
and `connection()` returns a descriptor whose shape differs by kind: a
host and port for a server, a path for a file. A consumer reaching a
SQLite silo really does open a file, and the abstraction should say so.

What is uniform, because it is genuinely uniform: every silo can be
created, can be asked whether it is reachable, can describe how to
reach it, and can be made to go away.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


class SiloError(Exception):
    """A silo could not be created, started, stopped, or reached."""


@dataclass(frozen=True)
class ConnectionDescriptor:
    """How to reach one silo.

    `kind` names the technology the way its own ecosystem does --
    "postgresql", "mariadb", "sqlite" -- and `details` carries whatever
    that technology needs. Deliberately a dict rather than a typed
    union: the shapes genuinely differ, a consumer of a file-based silo
    needs a path and one of a server needs a host and port, and a union
    with half its fields None at all times would be a worse lie than an
    open mapping that says what it has.

    Credentials appear here when the technology really has them. The
    database silos have none, because trust on a loopback socket is
    right for fictional data unreachable off the machine and a password
    would be ceremony every consumer then carries. The REST silo does
    carry a bearer token, because every small-business SaaS API is
    reached with one and a descriptor without it would not describe how
    to connect. An earlier version of this docstring asserted that no
    silo had credentials; that was true when they were all databases
    and stopped being true, which is why it says this instead.
    """

    kind: str
    details: dict[str, object] = field(default_factory=dict)

    def summary(self) -> str:
        """A one-line, human-facing form. For printing, not parsing.

        Every kind has to produce something useful here, which sounds
        obvious and was not: the REST silo answered with a bare "rest"
        because its descriptor carries a base_url rather than a host
        and a port, and a summary that omits where to connect is worse
        than none -- someone reads it, sees a silo listed, and has no
        idea where it is.
        """
        if "base_url" in self.details:
            return str(self.details["base_url"])
        if "port" in self.details:
            host, port = self.details.get("host"), self.details["port"]
            database = self.details.get("database", "")
            return f"{self.kind}://{host}:{port}/{database}"
        if "path" in self.details:
            return f"{self.kind}:{self.details['path']}"
        return self.kind


class Silo(ABC):
    """One system a business runs, presented as it is really reached."""

    #: Whether this kind of silo listens on a TCP port. False for
    #: file-based ones, which is not an edge case -- it is how SQLite
    #: and a folder of CSV genuinely work, and the port registry
    #: allocates only for silos that say True.
    requires_port: ClassVar[bool] = True

    #: How this silo names itself in a connection descriptor.
    kind: ClassVar[str]

    def __init__(self, name: str, data_dir: Path) -> None:
        #: The business's own name for the system: "pos", "books",
        #: "shop". Not a technology name -- two silos can share a
        #: technology and they are still different systems.
        self.name = name
        self.data_dir = Path(data_dir)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r})"

    @abstractmethod
    def create(self) -> None:
        """Bring the silo into existence, once.

        Initialises a cluster, creates a file, makes a directory. Not
        idempotent by design: a second call should refuse rather than
        quietly discard whatever is already there, because the thing
        already there is usually somebody's half-finished test.
        """

    @abstractmethod
    def start(self) -> None:
        """Make the silo reachable.

        A no-op for file-based silos, and that is allowed rather than
        an embarrassment: nothing has to be started for a file to be
        openable.
        """

    @abstractmethod
    def stop(self) -> None:
        """Stop the silo, if it is running. Quiet when it is not.

        Every implementation promises the outcome -- the silo is not
        running -- rather than the mechanism. This matters: a server
        that exits on its own between the check and the shutdown
        command must not turn a successful teardown into an error.
        """

    @abstractmethod
    def is_reachable(self) -> bool:
        """Whether something could connect right now."""

    @abstractmethod
    def connection(self, database: str | None = None) -> ConnectionDescriptor:
        """How to reach it.

        `database` is meaningful only for kinds that hold several. The
        parameter is on the contract rather than only on those kinds so
        a caller holding a Silo can ask without first working out which
        kind it has -- and the file and API kinds refuse a database
        rather than ignoring one, because silently dropping it would
        let a pack declare something that quietly has no effect.
        """

    def driver_errors(self) -> tuple[type[Exception], ...]:
        """What this silo's driver raises when the silo is at fault.

        So that a caller which has to tolerate failure can tolerate the
        right failures. Three places here must keep going when a query
        cannot run -- the oracle sampling a column that has been
        dropped, the drift history on a database that has never
        drifted, and a status display -- and all three used to catch
        Exception.

        That is not merely untidy. A KeyError from a mistyped watch, or
        a bug in this codebase, read exactly like a column having gone
        away: the instrument reported drift that had not happened, and
        a wrong answer is worse than a crash because nobody
        investigates it.

        Empty by default, because a silo with no driver has no such
        errors and should not pretend otherwise.
        """
        return ()

    @abstractmethod
    def terminate(self) -> None:
        """Make the silo abruptly unreachable, as a real outage would.

        Not an error path -- a deliberate capability. "The system went
        down" is a condition worth putting a consumer through, and it
        should happen rather than be mocked. What it means differs by
        kind: a server is killed without a clean shutdown; a file is
        moved aside, which is recoverable and is what a mis-fired
        backup script does in real life.
        """


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): this abstraction replaced a design in which a
# silo was a PostgreSQL instance. That was wrong for the reason the whole
# project exists to avoid -- one engine had been chosen and every domain was
# going to be expressed in it, partly because a downstream consumer had an
# adapter for it. Small businesses run MariaDB, SQLite, PostgreSQL and folders
# of CSV, often several at once, and all four are on Foundry's own connector
# list. The set is the intersection of what these businesses run with what a
# platform of this kind reads, not a set invented here.
#
# RESOLVED: requires_port is a class variable rather than an instance one, and
# defaults True. A silo's need for a port is a property of its technology, not
# of a particular deployment -- SQLite never needs one, PostgreSQL always does.
#
# RESOLVED: ConnectionDescriptor.details is an open mapping rather than a typed
# union. The shapes genuinely differ, and a union with half its fields None at
# any given moment is a worse lie than a mapping that carries what it has.
#
# DEFERRED (known, intentional, not yet built): no FileDropSilo (a folder of
# CSV/Excel that a pack writes into) and no RestSilo. Both are on Foundry's
# connector list, both are genuinely how small businesses integrate -- a bank
# export dropped on a share is more common than any database connection -- and
# both are next. They are not written yet because the abstraction above should
# be proven against two server silos and one file silo first; a fourth and
# fifth kind added before the first three are exercised would be guessing.
#
# DEFERRED: no credentials anywhere. Every silo here trusts loopback, which is
# right for fictional data unreachable off the machine and wrong the moment the
# simulator is used to exercise a consumer's credential handling. PostgreSQL
# and MariaDB both support real roles and grants; that is the natural next step
# and it belongs in the pack file, not here.
