"""Roadmap 13: does each engine give a consumer the right answers?

The same logical assertion against PostgreSQL and MariaDB. Where the
two differ, the difference is the finding -- a consumer has to be told
about it, and the test says what it is rather than picking a winner.

Nothing here imports `simulator`.
"""

import datetime
import decimal

from test_connection import connect, query


def quoted(name: str, kind: str) -> str:
    return f'"{name}"' if kind == "postgresql" else f"`{name}`"


def column(details, name):
    """One column's values, read the way a consumer would."""
    statement = (f"SELECT {quoted(name, details['kind'])} FROM "
                 f"{quoted('readings', details['kind'])}")
    return [row[0] for row in query(details, statement)]


# -- 13a: money -------------------------------------------------------

def test_money_arrives_as_an_exact_decimal(database):
    # Asserted as an exact type and scale, not with an epsilon. An
    # epsilon comparison passes on the bug it exists to catch.
    name, details = database
    amounts = column(details, "amount")
    assert amounts, name
    for amount in amounts:
        assert isinstance(amount, decimal.Decimal), f"{name}: {type(amount)}"
        assert amount.as_tuple().exponent == -4, f"{name}: {amount}"


def test_money_sums_without_drifting(database):
    # A float column would disagree with itself here. Summing in the
    # engine and summing in Python must give the same answer.
    name, details = database
    in_engine = query(details, "SELECT sum(amount) FROM " +
                      quoted("readings", details["kind"]))[0][0]
    in_python = sum(column(details, "amount"))
    assert in_engine == in_python, name


# -- 13b: timestamps and offsets ---------------------------------------

def test_the_two_engines_disagree_about_offsets_and_that_is_documented(connections):
    # NOT a defect to fix. PostgreSQL's TIMESTAMPTZ keeps the offset;
    # MariaDB's DATETIME does not, and its TIMESTAMP would convert
    # through the session time zone and expire in 2038, so DATETIME is
    # the honest choice. A consumer must not silently localise the
    # naive one.
    postgres = column(connections["ops"], "taken_at")[0]
    mariadb = column(connections["shop"], "taken_at")[0]

    assert isinstance(postgres, datetime.datetime)
    assert isinstance(mariadb, datetime.datetime)
    assert postgres.tzinfo is not None, "PostgreSQL should keep the offset"
    assert mariadb.tzinfo is None, "MariaDB DATETIME carries no offset"


def test_dates_are_dates_and_not_timestamps(database):
    name, details = database
    days = column(details, "on_day")
    assert days, name
    for day in days:
        assert isinstance(day, datetime.date), name
        assert not isinstance(day, datetime.datetime), f"{name}: got a timestamp"


# -- 13c: booleans -----------------------------------------------------

def test_booleans_read_differently_on_each_engine(connections):
    # A real BOOLEAN on PostgreSQL; TINYINT(1) on MariaDB, so the same
    # column reads True and 1. A consumer assuming both are `bool`
    # breaks on one of them.
    postgres = column(connections["ops"], "settled")
    mariadb = column(connections["shop"], "settled")

    assert {type(value) for value in postgres} == {bool}
    assert {type(value) for value in mariadb} == {int}
    # Both are truthy in the same way, which is what saves a consumer
    # that tests `if settled:` rather than `if settled is True:`.
    assert set(map(bool, postgres)) == set(map(bool, mariadb)) == {True, False}


# -- 13d: unicode ------------------------------------------------------

def test_unicode_survives_the_engine_and_the_driver(database):
    # utf8mb4 versus three-byte utf8 is a real trap, and so is a client
    # defaulting to latin1.
    name, details = database
    labels = set(column(details, "label"))
    assert "Café Solstråle 🌞" in labels, f"{name}: got {labels}"
    assert "Ωmega" in labels, name


# -- 13e: null, absent and empty ---------------------------------------

def test_null_is_none_and_not_an_empty_string(database):
    # Three different things a consumer must keep apart. `note` is
    # nullable and nothing ever writes it.
    name, details = database
    notes = column(details, "note")
    assert notes, name
    assert all(note is None for note in notes), name
    assert not any(note == "" for note in notes), name


# -- 13f: column order -------------------------------------------------

def test_column_order_is_what_the_pack_declared(database):
    # Observable through SELECT *, so a consumer reading by position
    # depends on it. It is stable here, which is what makes reading by
    # position merely unwise rather than broken.
    name, details = database
    connection = connect(details)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM " + quoted("readings", details["kind"]))
            described = [item[0] for item in cursor.description]
    finally:
        connection.close()
    assert described == ["reading_id", "label", "note", "amount", "settled",
                         "taken_at", "on_day", "quantity", "order"], name


# -- 13g: reserved words -----------------------------------------------

def test_a_column_named_after_a_reserved_word_is_readable(database):
    # `order` is reserved on both engines. A consumer that does not
    # quote identifiers cannot read this table at all -- which is the
    # point of the column existing.
    name, details = database
    values = column(details, "order")
    assert len(values) > 0, name


# -- 13h: reading a lot ------------------------------------------------

def test_a_full_scan_returns_every_row(database):
    name, details = database
    counted = query(details, "SELECT count(*) FROM " +
                    quoted("readings", details["kind"]))[0][0]
    fetched = len(column(details, "reading_id"))
    assert counted == fetched > 0, name


def test_both_engines_saw_the_same_business(connections):
    # The two silos run the same events at the same rate from the same
    # seed, so they should be in the same ballpark. Wildly different
    # counts would mean one engine's writes were being lost.
    counts = {}
    for name in ("ops", "shop"):
        details = connections[name]
        counts[name] = query(details, "SELECT count(*) FROM " +
                             quoted("readings", details["kind"]))[0][0]
    assert min(counts.values()) > 0
    assert max(counts.values()) < min(counts.values()) * 3, counts
