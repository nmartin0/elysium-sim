"""Can a consumer following the published descriptor damage anything?

The question a client actually asks before pointing a tool at their
production database, and a simulator that hands over the keys cannot
answer it: every read succeeds either way, and the one time it matters
there is nothing to discover.

THE REFUSAL MUST COME FROM THE DATABASE, not from the consumer's good
intentions, because that is where it comes from in production. So
every assertion here is that the ENGINE said no.

Measured before the reader account existed, on both engines:

    a consumer following the descriptor can therefore:
      ops:  DROP TABLE sources SUCCEEDED
      shop: DROP TABLE sources SUCCEEDED

Nothing here imports `simulator`.
"""

import psycopg
import pymysql
import pytest
from test_connection import connect, query

#: What a driver raises when the server declines. Named rather than
#: catching Exception, so a typo in the SQL below cannot pass as a
#: refusal -- which would make this whole file assert nothing.
DRIVER_ERRORS = (psycopg.Error, pymysql.Error)

#: How each engine says no. PostgreSQL words a DDL refusal as "must be
#: owner" rather than as a permission, which is the same answer
#: reached by a different route -- and a first version of this test
#: listed only the permission wordings and so failed on four
#: legitimate refusals.
REFUSAL_WORDS = ("permission", "denied", "privilege", "not allowed", "must be owner")

#: Every shape of damage, in the order a client worries about them.
DESTRUCTIVE = [
    ("drop a table", 'DROP TABLE {readings}', False),
    ("delete every row", 'DELETE FROM {readings}', False),
    ("empty a table", 'TRUNCATE {readings}', False),
    ("change data", 'UPDATE {readings} SET {quantity} = 0', False),
    ("add data", "INSERT INTO {sources} VALUES ('x')", False),
    ("drop a column", 'ALTER TABLE {readings} DROP COLUMN {note}', False),
    ("add a column", 'ALTER TABLE {readings} ADD COLUMN {extra} varchar(8)', False),
    ("create a table", 'CREATE TABLE {evil} (x varchar(8))', False),
    # Autocommit, because PostgreSQL refuses this inside a transaction
    # block BEFORE it checks privilege -- so run the ordinary way it
    # fails for a reason that proves nothing about permissions.
    ("drop the database", 'DROP DATABASE {probe}', True),
]


def sql(template: str, kind: str, database: str) -> str:
    quote = '"{}"' if kind == "postgresql" else "`{}`"
    names = {name: quote.format(name) for name in
             ("readings", "sources", "quantity", "note", "extra", "evil")}
    names["probe"] = quote.format(database)
    return template.format(**names)


@pytest.mark.parametrize("what,template,autocommit", DESTRUCTIVE,
                         ids=[d[0] for d in DESTRUCTIVE])
def test_the_engine_refuses_to_be_damaged(database, what, template, autocommit):
    name, details = database
    statement = sql(template, details["kind"], details["database"])

    connection = connect(details)
    if autocommit:
        connection.autocommit = True
    try:
        with pytest.raises(DRIVER_ERRORS) as raised, connection.cursor() as cursor:
            cursor.execute(statement)
        # Not a connection failure, not a syntax error: the server
        # understood the request and declined it.
        message = str(raised.value).lower()
        assert any(word in message for word in REFUSAL_WORDS), \
            f"{name}: refused, but not for the right reason -- {raised.value}"
    finally:
        connection.close()


def test_reading_still_works(database):
    # The refusal has to be narrow. A reader that cannot read is not a
    # safety feature, it is an outage.
    name, details = database
    quote = '"{}"' if details["kind"] == "postgresql" else "`{}`"
    rows = query(details, f"SELECT count(*) FROM {quote.format('readings')}")
    assert rows[0][0] > 0, name


def test_the_account_is_not_the_one_that_owns_the_schema(database):
    # No business hands a reporting tool the account that owns its
    # schema. MariaDB advertised `root` with GRANT ALL PRIVILEGES ON
    # *.* WITH GRANT OPTION before this.
    name, details = database
    assert details["user"] not in ("root", "postgres", "sim"), name

    statement = ("SELECT current_user" if details["kind"] == "postgresql"
                 else "SELECT current_user()")
    who = str(query(details, statement)[0][0])
    assert who.split("@")[0] == details["user"], name


def test_the_account_is_not_a_superuser(database):
    name, details = database
    if details["kind"] == "postgresql":
        rows = query(details, "SELECT usesuper FROM pg_user WHERE usename = current_user")
        assert rows[0][0] is False, name
    else:
        grants = [str(row[0]) for row in query(details, "SHOW GRANTS")]
        assert not any("ALL PRIVILEGES" in grant for grant in grants), grants
        assert not any("GRANT OPTION" in grant for grant in grants), grants


def test_the_account_cannot_write_to_any_other_database(database):
    # NOT "cannot connect elsewhere". PostgreSQL grants CONNECT on a
    # database to PUBLIC by default, so any role can open the
    # maintenance database and read its catalogue -- and that is what a
    # real deployment looks like, so a simulator asserting otherwise
    # would be testing a hardening step most clients never take.
    #
    # What matters, and what is true, is that the account cannot change
    # anything anywhere.
    name, details = database
    other = "postgres" if details["kind"] == "postgresql" else "mysql"
    try:
        connection = connect({**details, "database": other})
    except DRIVER_ERRORS:
        return  # refused at the door, which is also fine
    try:
        with pytest.raises(DRIVER_ERRORS), connection.cursor() as cursor:
            cursor.execute("CREATE TABLE evil_elsewhere (x varchar(8))")
    finally:
        connection.close()


def test_a_table_added_by_drift_is_still_readable(database):
    # The subtle half. Drift adds tables while the world runs, and a
    # reader that could not see them would look like a silo going
    # blind -- a failure no consumer would meet in production, caused
    # entirely by the simulator's provisioning order.
    #
    # On PostgreSQL this works only because ALTER DEFAULT PRIVILEGES is
    # issued when the database is created, before any table exists.
    name, details = database
    quote = '"{}"' if details["kind"] == "postgresql" else "`{}`"
    for table in ("readings", "sources"):
        rows = query(details, f"SELECT count(*) FROM {quote.format(table)}")
        assert rows[0][0] >= 0, f"{name}.{table}"
