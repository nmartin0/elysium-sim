"""
reader.py  (the account a consumer is actually given)

WHAT THIS FIXES, AND IT WAS NOT THEORETICAL. Until this existed, the
connection descriptor a consumer follows advertised the account the
simulator itself writes with. Measured on a running probe world:

    postgres CURRENT_USER: sim, usesuper=True
    mariadb  CURRENT_USER: root@localhost
    mariadb  grants: GRANT ALL PRIVILEGES ON *.* WITH GRANT OPTION
    a consumer following the descriptor can therefore:
      ops:  DROP TABLE sources SUCCEEDED
      shop: DROP TABLE sources SUCCEEDED

Two things wrong with that at once.

IT IS NOT AUTHENTIC. No business hands a reporting tool the account
that owns its schema. A reader gets SELECT on the tables it needs and
nothing else, and a simulator whose databases are meant to stand in for
real ones has to present that shape or it is testing against a
privilege level no deployment would give.

AND IT MAKES THE INTERESTING QUESTION UNASKABLE. "Will this consumer
damage a client's database" cannot be answered by a simulator that
hands over the keys: every read succeeds either way, and the one time
it matters there is nothing to discover. The refusal has to come from
the DATABASE, not from the consumer's good intentions, because that is
where it comes from in production.

WHY A ROLE AND NOT A SETTING. PostgreSQL has
`default_transaction_read_only` and MariaDB has `--read-only`, and both
were rejected: they are server-wide, so the simulator could not write
either. A grant is per-account, which is the real mechanism a real
deployment uses, and it leaves the simulator's own account untouched.

FUTURE TABLES ARE THE SUBTLE PART. Drift adds tables while the world
runs, and a reader that could not see them would report a silo going
blind for a reason no consumer would meet in production. MariaDB's
database-wide grant covers them; PostgreSQL needs ALTER DEFAULT
PRIVILEGES, which applies only to tables created LATER by the role it
names -- so it has to be issued before the schema is applied, not
after.
"""

from simulator.silo import SiloError

#: The account a consumer is given. Named for what it can do rather
#: than for the tool using it, because a second consumer would want the
#: same account rather than one of its own.
READER = "reader"


def provision_postgres(silo, database: str, owner: str) -> None:
    """Give the reader SELECT on this database and nothing else.

    CONNECT and USAGE first, because without them the grant lands on
    tables the account cannot reach -- a refusal at the door rather
    than at the table, which reads as an outage rather than a
    permission.
    """
    statements = [
        # Idempotent, and here rather than at instance creation because
        # a role needs a running server and `initdb` has not started one
        # yet. Roles are per-cluster, so the second database to be
        # provisioned finds it already there.
        f"""DO $$ BEGIN
               IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{READER}')
               THEN CREATE ROLE "{READER}" LOGIN;
               END IF;
             END $$""",
        f'GRANT CONNECT ON DATABASE "{database}" TO "{READER}"',
        f'GRANT USAGE ON SCHEMA public TO "{READER}"',
        f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{READER}"',
        # Tables that do not exist yet, which drift will create. This
        # applies only to tables the owner makes AFTER it is issued, so
        # ordering matters: it is issued when the database is created,
        # before any schema is applied.
        f'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner}" IN SCHEMA public '
        f'GRANT SELECT ON TABLES TO "{READER}"',
    ]
    _run(silo, database, statements, autocommit=False)


def provision_mariadb(silo, database: str) -> None:
    """Give the reader SELECT on this database and nothing else.

    Scoped to 127.0.0.1 rather than '%': the silo only listens on
    loopback, so an account that could connect from anywhere would be
    advertising a reach it does not have.

    A database-wide grant covers tables that do not exist yet, so drift
    adding one needs no further grant -- which is the same thing
    ALTER DEFAULT PRIVILEGES buys on PostgreSQL, spelled far more
    simply.
    """
    _run(silo, database, [
        f"CREATE USER IF NOT EXISTS '{READER}'@'127.0.0.1'",
        f"GRANT SELECT ON `{database}`.* TO '{READER}'@'127.0.0.1'",
        "FLUSH PRIVILEGES",
    ], autocommit=True)


def _run(silo, database: str, statements: list[str], *, autocommit: bool) -> None:
    try:
        with silo.connect(database, autocommit=autocommit) as connection:
            with connection.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)
            if not autocommit:
                connection.commit()
    except Exception as error:  # noqa: BLE001 -- driver errors differ per engine
        raise SiloError(
            f"{silo.name}: could not provision the {READER!r} account -- {error}"
        ) from error


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: a grant rather than a server setting. PostgreSQL's
# default_transaction_read_only and MariaDB's --read-only are server-wide, so
# the simulator could not write either. A grant is per-account, which is what a
# real deployment uses.
#
# RESOLVED: the PostgreSQL default-privileges grant is issued when the database
# is created, before any schema is applied, because it affects only tables the
# owner creates AFTER it. Issued later, every existing table would be readable
# and every drifted-in table would not -- the exact opposite of a useful
# failure, since it would look like drift breaking a consumer when it was the
# simulator's provisioning order.
#
# DEFERRED (known, intentional, not yet built): no password. The silos trust
# loopback and the data is fictional, so a password would be ceremony every
# consumer then carries in its configuration. It becomes worth having the
# moment the simulator is used to exercise a consumer's credential handling,
# which is roadmap 6b.
#
# DEFERRED: one reader for the whole database rather than per-table grants. A
# real deployment often gives a reporting tool access to some tables and not
# others, and a consumer meeting a table it can see in the catalogue but cannot
# select from is a genuine and nasty failure mode worth simulating. It needs a
# way for a pack to say which tables are readable.
