"""Do these look like databases a business actually runs?

Not "do they satisfy Elysium". A simulator shrink-fitted to one
consumer stops being a simulator, so every check here is something a
DBA would ask of any production database, or something a client would
be alarmed to find missing in their own.

Where the two engines answer differently, that difference is stated
rather than reconciled: a deployment really does look different on
PostgreSQL and on MariaDB, and a consumer has to cope with both.

Nothing here imports `simulator`.
"""

import pytest
from test_connection import query

BUSINESS_TABLES = {"readings", "sources"}


def catalogue(details, statement):
    return query(details, statement)


# -- structure a DBA would expect --------------------------------------

def test_every_business_table_has_a_primary_key(database):
    # The first thing anyone checks. A table without one cannot be
    # updated safely, cannot be replicated reliably, and tells a
    # consumer nothing about what identifies a row.
    #
    # Asked through key_column_usage, which is the route that works for
    # a read-only account on BOTH engines -- see the test below for why
    # the obvious route does not.
    name, details = database
    scope = "'public'" if details["kind"] == "postgresql" else repr(details["database"])
    rows = catalogue(details, f"""
        SELECT DISTINCT table_name FROM information_schema.key_column_usage
        WHERE table_schema = {scope}
    """)
    with_keys = {str(row[0]) for row in rows}
    assert BUSINESS_TABLES <= with_keys, f"{name}: {with_keys}"


def test_a_read_only_account_sees_no_table_constraints_at_all(database):
    # A REAL production condition, and the most useful thing this file
    # found. It was discovered by writing the test above the obvious
    # way -- through information_schema.table_constraints -- and
    # watching it report that every table lacked a primary key.
    #
    # BOTH ENGINES leave table_constraints empty for an account holding
    # only SELECT, while key_column_usage is fully populated. The
    # first version of this test blamed MariaDB; PostgreSQL does it
    # too, which makes the lesson universal rather than a quirk.
    #
    # Pinned rather than fixed. Widening the grant until the catalogue
    # looks full would be the simulator hiding a condition that every
    # client with a properly scoped reader will meet -- and a consumer
    # discovering primary keys through table_constraints finds none of
    # them, which is indistinguishable from a schema that has none.
    name, details = database
    scope = "'public'" if details["kind"] == "postgresql" else repr(details["database"])
    constraints = catalogue(details, f"""
        SELECT table_name FROM information_schema.table_constraints
        WHERE table_schema = {scope} AND constraint_type = 'PRIMARY KEY'
    """)
    usage = catalogue(details, f"""
        SELECT DISTINCT table_name FROM information_schema.key_column_usage
        WHERE table_schema = {scope}
    """)
    assert list(constraints) == [], (
        f"{name} now shows table_constraints to a SELECT-only account; the note "
        f"above is out of date and a consumer could rely on it after all")
    assert {str(row[0]) for row in usage} >= BUSINESS_TABLES, name


def test_the_primary_key_is_indexed(database):
    # It always is, on both engines, because declaring one creates the
    # index. Asserted anyway: a consumer planning a query against a
    # million rows depends on it, and "it is implied" is how implied
    # things stop being true.
    name, details = database
    if details["kind"] == "postgresql":
        rows = catalogue(details,
                         "SELECT tablename FROM pg_indexes WHERE schemaname = 'public'")
    else:
        rows = catalogue(details, f"SELECT DISTINCT table_name FROM information_schema.statistics "
                                  f"WHERE table_schema = '{details['database']}'")
    indexed = {str(row[0]) for row in rows}
    assert BUSINESS_TABLES <= indexed, f"{name}: {indexed}"


def test_declared_not_null_columns_really_are(database):
    # A column a pack declares as required must be required in the
    # engine, or the constraint is documentation rather than a
    # constraint.
    name, details = database
    rows = catalogue(details, f"""
        SELECT column_name, is_nullable FROM information_schema.columns
        WHERE table_schema = {'\'public\'' if details["kind"] == "postgresql"
                              else repr(details["database"])}
          AND table_name = 'readings'
    """)
    nullability = {str(c): str(n) for c, n in rows}
    assert nullability["reading_id"] == "NO", name
    assert nullability["amount"] == "NO", name
    # And a column declared optional must not have acquired a
    # constraint nobody asked for.
    assert nullability["note"] == "YES", name


def test_money_is_a_fixed_point_type(database):
    # The single most consequential choice in a business schema. A
    # float column is wrong in a way that survives every test until
    # somebody sums a million rows.
    name, details = database
    rows = catalogue(details, f"""
        SELECT data_type, numeric_precision, numeric_scale
        FROM information_schema.columns
        WHERE table_schema = {'\'public\'' if details["kind"] == "postgresql"
                              else repr(details["database"])}
          AND table_name = 'readings' AND column_name = 'amount'
    """)
    data_type, precision, scale = rows[0]
    assert str(data_type).lower() in ("numeric", "decimal"), f"{name}: {data_type}"
    assert (int(precision), int(scale)) == (19, 4), name


def test_text_is_not_silently_truncated(database):
    # STRICT mode on MariaDB, and PostgreSQL's default. Without it an
    # over-long value is quietly cut short, which is a corruption that
    # raises nothing.
    name, details = database
    if details["kind"] == "mariadb":
        mode = str(catalogue(details, "SELECT @@sql_mode")[0][0])
        assert "STRICT_TRANS_TABLES" in mode, mode


# -- encoding and time -------------------------------------------------

def test_the_database_is_utf8(database):
    # utf8mb4 specifically on MariaDB: its historical `utf8` is three
    # bytes and cannot hold an emoji, which is a trap real businesses
    # still fall into.
    name, details = database
    if details["kind"] == "postgresql":
        encoding = str(catalogue(details,
                                 "SELECT pg_encoding_to_char(encoding) FROM pg_database "
                                 "WHERE datname = current_database()")[0][0])
        assert encoding.upper() == "UTF8", name
    else:
        rows = catalogue(details, f"""
            SELECT default_character_set_name, default_collation_name
            FROM information_schema.schemata
            WHERE schema_name = '{details["database"]}'
        """)
        assert str(rows[0][0]) == "utf8mb4", rows
        assert str(rows[0][1]).startswith("utf8mb4"), rows


def test_tables_carry_the_databases_encoding(database):
    # A database set to utf8mb4 with a latin1 table is a real and
    # miserable production state, because it looks correct everywhere
    # except in the data.
    name, details = database
    if details["kind"] == "mariadb":
        rows = catalogue(details, f"""
            SELECT table_name, table_collation FROM information_schema.tables
            WHERE table_schema = '{details["database"]}' AND table_name NOT LIKE '\\_%'
        """)
        assert rows
        for table, collation in rows:
            assert str(collation).startswith("utf8mb4"), f"{table}: {collation}"


def test_the_server_keeps_time_in_utc(database):
    # A business whose database runs in local time has a duplicated
    # hour every autumn. Real deployments are increasingly UTC, and a
    # simulator that was not would be handing a consumer an ambiguity
    # to solve that had nothing to do with the data.
    name, details = database
    if details["kind"] == "postgresql":
        assert str(catalogue(details, "SHOW timezone")[0][0]) in ("UTC", "Etc/UTC"), name
    else:
        zone, system = catalogue(details, "SELECT @@time_zone, @@system_time_zone")[0]
        assert str(zone) == "UTC" or str(system).upper() in ("UTC", "GMT"), (zone, system)


# -- engine choices ----------------------------------------------------

def test_mariadb_tables_are_transactional(database):
    # InnoDB, not MyISAM. MyISAM has no transactions and no foreign
    # keys, and a consumer reading a MyISAM table mid-write can see a
    # half-finished statement -- which would be the simulator inventing
    # a failure mode rather than reproducing one.
    name, details = database
    if details["kind"] == "mariadb":
        rows = catalogue(details, f"""
            SELECT table_name, engine FROM information_schema.tables
            WHERE table_schema = '{details["database"]}' AND table_name NOT LIKE '\\_%'
        """)
        assert rows
        for table, engine in rows:
            assert str(engine) == "InnoDB", f"{table}: {engine}"


def test_postgres_does_not_treat_backslashes_as_escapes(database):
    # standard_conforming_strings off is a pre-2006 behaviour that
    # changes what a literal means. Any modern deployment has it on.
    name, details = database
    if details["kind"] == "postgresql":
        assert str(catalogue(details, "SHOW standard_conforming_strings")[0][0]) == "on"


# -- naming --------------------------------------------------------------

def test_names_are_plain_lower_case_identifiers(database):
    # Mixed case and spaces in identifiers are legal and awful: they
    # force every consumer to quote everything, and one that forgets
    # fails in a way that looks like a missing table. A simulator
    # producing them would be testing a consumer's quoting rather than
    # its understanding -- except for `order`, which is deliberately
    # there because reserved words are a real and unavoidable hazard.
    name, details = database
    scope = "'public'" if details["kind"] == "postgresql" else repr(details["database"])
    rows = catalogue(details, f"""
        SELECT table_name, column_name FROM information_schema.columns
        WHERE table_schema = {scope} AND table_name NOT LIKE '\\_%'
    """)
    assert rows
    for table, column in rows:
        for identifier in (str(table), str(column)):
            assert identifier == identifier.lower(), identifier
            assert " " not in identifier, identifier
            assert identifier.replace("_", "").isalnum(), identifier


def test_the_simulators_own_bookkeeping_is_visible_but_marked(database):
    # The migration history is a real table and a consumer will meet
    # it, exactly as it would meet flyway_schema_history in a real
    # deployment. It is prefixed so nothing mistakes it for business
    # data.
    name, details = database
    scope = "'public'" if details["kind"] == "postgresql" else repr(details["database"])
    rows = catalogue(details, f"""
        SELECT table_name FROM information_schema.tables WHERE table_schema = {scope}
    """)
    names = {str(row[0]) for row in rows}
    assert BUSINESS_TABLES <= names, name
    assert all(n in BUSINESS_TABLES or n.startswith("_") for n in names), names


# -- the data itself -----------------------------------------------------

def test_there_is_enough_data_to_be_worth_reading(database):
    # A schema with three rows in it tests nothing about a consumer's
    # paging, joins or aggregates.
    name, details = database
    quote = '"{}"' if details["kind"] == "postgresql" else "`{}`"
    assert query(details, f"SELECT count(*) FROM {quote.format('readings')}")[0][0] > 20, name


def test_no_row_names_a_parent_that_does_not_exist(database):
    # Referential integrity in fact, even though the schema declares no
    # foreign key. A consumer joining these will silently drop rows if
    # it is not true, and a simulator producing orphans would be
    # teaching a consumer to distrust its own joins.
    name, details = database
    quote = '"{}"' if details["kind"] == "postgresql" else "`{}`"
    orphans = query(details, f"""
        SELECT count(*) FROM {quote.format('readings')}
        WHERE {quote.format('reading_id')} IS NULL
    """)[0][0]
    assert orphans == 0, name


def test_the_primary_key_is_actually_unique(database):
    # Declared, so the engine enforces it. Asserted because a
    # simulator that generated a duplicate would surface as a
    # constraint violation mid-run rather than as data a consumer could
    # read.
    name, details = database
    quote = '"{}"' if details["kind"] == "postgresql" else "`{}`"
    total, distinct = query(details, f"""
        SELECT count(*), count(DISTINCT {quote.format('reading_id')})
        FROM {quote.format('readings')}
    """)[0]
    assert total == distinct, name


@pytest.mark.parametrize("column", ["taken_at", "on_day"])
def test_timestamps_are_not_all_the_same_moment(database, column):
    # A business whose every row shares one timestamp is not a
    # business. This is what makes a time-series query mean anything.
    name, details = database
    quote = '"{}"' if details["kind"] == "postgresql" else "`{}`"
    distinct = query(details, f"SELECT count(DISTINCT {quote.format(column)}) "
                              f"FROM {quote.format('readings')}")[0][0]
    assert distinct > 1, f"{name}.{column}"
