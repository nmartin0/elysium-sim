"""
connection.py  (holding one connection open, for either SQL engine)

Why this is a collaborator and not a base class. Measured, method by
method, between PostgresSilo and MariaDbSilo:

    session          100%   (35 lines, verbatim, comment included)
    _await_death     100%   (19 lines)
    _recorded_pid     99%
    connection        93%
    connect           70%
    ---
    create            36%
    start             30%
    stop              22%

The two halves of that table are different problems. A `SqlSilo`
parent would have to carry `create`, `start` and `stop` too, and those
genuinely differ -- one engine has a control program that daemonises
and waits, the other does not; one is initialised with `initdb`, the
other with `mariadb-install-db`. Sharing them would mean a template
method per difference and a parent that is two implementations
interleaved.

So the shared half moves out and the different half stays put. Each
silo hands over an `open` callable that knows its own driver, and gets
back the part neither needed to write twice.

What it actually does is decide, on every request for a connection,
whether one is already open that will serve. That sounds trivial and
the reasoning is not: reusing a connection with the wrong database
would silently read the wrong tables, and reusing a non-autocommit one
for a statement that needs autocommit fails on PostgreSQL, which
refuses CREATE DATABASE inside a transaction block.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from simulator.silo import ConnectionDescriptor


@dataclass
class SharedConnection:
    """One connection, held open across a block of work.

    Measured on this machine: opening a connection per statement costs
    62.9ms, against 0.42ms to run the same statement on one already
    open -- 149 times the cost of the query. A tick writing a few
    hundred rows spent almost all of its time in handshakes.
    """

    #: Opens a brand new connection. A context manager, so the caller
    #: owns closing it; the silo supplies this because only the silo
    #: knows its driver.
    open: Callable[..., Any]

    _active: Any = field(default=None, init=False)
    _active_database: str | None = field(default=None, init=False)

    @contextmanager
    def session(self, database: str) -> Iterator[Any]:
        """Hold one connection open for a block, committing at the end.

        Everything inside commits together, which is more honest than
        committing piecemeal: a sale, its lines and the stock it moved
        are one event and should not be separately visible.

        Re-entrant. A caller opening a session around code that opens
        its own joins the outer transaction rather than starting a
        second one -- which matters because a second connection would
        commit independently, so an outer failure would roll back only
        part of the work.
        """
        if self._active is not None:
            yield self._active
            return
        with self.open(database, autocommit=False) as connection:
            self._active = connection
            self._active_database = database
            try:
                yield connection
                connection.commit()
            finally:
                self._active = None
                self._active_database = None

    @contextmanager
    def use(self, database: str, *, autocommit: bool = False) -> Iterator[Any]:
        """A connection for one piece of work, reusing the session's if it fits.

        "If it fits" is two conditions and both are load-bearing.
        Reusing a connection open on a different database would
        silently read the wrong tables. Reusing a non-autocommit one
        for something that needs autocommit fails on PostgreSQL, which
        refuses CREATE DATABASE inside a transaction block -- so
        provisioning opens its own.

        Whether it commits depends on which of those happened, and that
        asymmetry is the subtlest thing in this file. See below.
        """
        if (self._active is not None and self._active_database == database
                and not autocommit):
            # Borrowed, and deliberately not committed: the session
            # owns the transaction boundary, and committing here would
            # end it early -- which is exactly how an "atomic per tick"
            # claim quietly becomes false.
            yield self._active
            return
        with self.open(database, autocommit=autocommit) as connection:
            yield connection
            # Committed, because this connection is ours and nothing
            # else will. Dropping this line was the one real bug in
            # extracting this module, and twenty tests found it at
            # once: schemas were created and rolled back on close, so
            # the engine reported an empty catalogue for a table that
            # had just been declared.
            connection.commit()


def server_descriptor(kind: str, port: int, database: str,
                      user: str) -> ConnectionDescriptor:
    """How a consumer reaches a database served over a port.

    Both SQL silos answer this identically; only the kind differs.
    Two accounts are named: `user` reads and `writer_user` writes.

    No password here, and not because there is none: a silo has no
    seed and should not learn about one, so the world adds the
    credentials when it publishes its connections. See
    World.connections.
    """
    from simulator.silos.reader import WRITER

    return ConnectionDescriptor(kind=kind, details={
        "host": "127.0.0.1",
        "port": port,
        "database": database,
        "user": user,
        # Advertised separately, because a consumer with a governed
        # write path needs an account that can take it and a read path
        # that must not be able to. Naming both is how a real
        # deployment hands them over.
        "writer_user": WRITER,
    })


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: a collaborator rather than a base class, and the split follows a
# measurement rather than a preference. session() was 100% identical between
# the two SQL silos and create/start/stop were 22-36% similar; a parent would
# have had to carry both halves. The earlier decision recorded in mariadb.py --
# "not a subclass of PostgresSilo" -- was right about the lifecycle and wrong
# about the connection, and this is the correction.
#
# DEFERRED (known, intentional, not yet built): this is not a pool. One
# connection is held, not several, and two threads sharing a silo would share
# that connection -- which is fine because a tick is single-threaded and is a
# real constraint the moment anything runs ticks concurrently.
#
# DEFERRED: no retry. A connection that drops mid-session surfaces as whatever
# the driver raises. That is arguably correct for a tool whose purpose includes
# making databases go away on purpose, but a transient failure during a long
# run is a different thing from a deliberate terminate() and nothing tells them
# apart.
