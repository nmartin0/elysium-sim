"""Tests for reading back what each account actually did.

The parsers are where the bugs were, and both were silent: they
produced plausible output that attributed statements to the wrong
account. So these work against real log text rather than a live world
-- a fixture that reproduces the exact shapes both engines emit,
including the ones that broke.
"""

import textwrap
from datetime import UTC, datetime

import pytest

from simulator.audit import (
    DANGEROUS,
    DESTRUCTIVE,
    OWNERS,
    READ,
    SCHEMA,
    WRITE,
    classify,
    consumers_only,
    read_mariadb_log,
    read_postgres_log,
    summarise,
)

POSTGRES_LOG = textwrap.dedent("""\
    2026-09-16 18:48:05.853 UTC|sim|probe|LOG:  statement: CREATE TABLE "readings" (
    \t"reading_id" TEXT NOT NULL
    )
    2026-09-16 18:48:06.122 UTC|reader|probe|LOG:  statement: SELECT count(*) FROM "readings"
    2026-09-16 18:48:06.123 UTC|writer|probe|LOG:  statement: UPDATE "readings" SET "order" = 1
    2026-09-16 18:48:06.130 UTC|reader|probe|LOG:  statement: DROP TABLE "sources"
    2026-09-16 18:48:06.130 UTC|reader|probe|ERROR:  must be owner of table sources
    2026-09-16 18:48:06.130 UTC|reader|probe|STATEMENT:  DROP TABLE "sources"
    """)

#: The real shape, tabs and all. MariaDB stamps only the FIRST line of
#: each second and leaves the rest to start with whitespace -- the two
#: forms that between them caused both parser bugs.
MARIADB_LOG = (
    "/usr/sbin/mariadbd, Version: 10.11.14-MariaDB (Ubuntu). started with:\n"
    "Tcp port: 54937  Unix socket: /tmp/sim.sock\n"
    "Time\t\t    Id Command\tArgument\n"
    "260916 18:49:29\t     3 Connect\troot@localhost on probe using TCP/IP\n"
    "\t\t     3 Query\tINSERT INTO `readings` (`a`,`b`) VALUES ('1','2'),\n"
    "('3','4')\n"
    "\t\t    11 Connect\treader@localhost on probe using TCP/IP\n"
    "\t\t    11 Query\tSELECT count(*) FROM `readings`\n"
    "\t\t    11 Query\tDROP TABLE `sources`\n"
    "\t\t    11 Quit\n"
)


# -- classification ----------------------------------------------------

@pytest.mark.parametrize("statement,expected", [
    ("SELECT 1", READ),
    ("  select count(*) from x", READ),
    ("WITH a AS (SELECT 1) SELECT * FROM a", READ),
    ("INSERT INTO t VALUES (1)", WRITE),
    ("UPDATE t SET x = 1", WRITE),
    ("DELETE FROM t", DESTRUCTIVE),
    ("TRUNCATE t", DESTRUCTIVE),
    ('DROP TABLE "t"', DESTRUCTIVE),
    ("ALTER TABLE t ADD COLUMN x TEXT", SCHEMA),
    ("CREATE TABLE t (x TEXT)", SCHEMA),
    ("wibble", "other"),
])
def test_a_statement_is_classified_by_what_it_does(statement, expected):
    assert classify(statement) == expected


def test_everything_that_could_change_or_destroy_is_dangerous():
    # Named once, so the question cannot be asked slightly differently
    # somewhere else.
    assert DANGEROUS == {WRITE, DESTRUCTIVE, SCHEMA, "admin"}
    assert READ not in DANGEROUS


# -- PostgreSQL --------------------------------------------------------

def test_postgres_statements_carry_their_account(tmp_path):
    path = tmp_path / "postgres.log"
    path.write_text(POSTGRES_LOG)
    statements = list(read_postgres_log(path))

    assert [s.account for s in statements] == ["sim", "reader", "writer", "reader"]
    assert [s.kind for s in statements] == [SCHEMA, READ, WRITE, DESTRUCTIVE]
    assert all(s.database == "probe" for s in statements)


def test_a_refused_statement_is_recorded_once_and_marked(tmp_path):
    # THE entry that matters: a tool whose DROP was refused and one
    # that never tried look identical from outside, and only one of
    # them is reassuring.
    #
    # PostgreSQL reports an error as two further lines -- ERROR then
    # STATEMENT -- after the statement was already logged on receipt.
    # Counting those as new statements would report the attempt three
    # times.
    path = tmp_path / "postgres.log"
    path.write_text(POSTGRES_LOG)
    statements = list(read_postgres_log(path))

    drops = [s for s in statements if s.kind is DESTRUCTIVE]
    assert len(drops) == 1
    assert drops[0].refused is True
    assert drops[0].text == 'DROP TABLE "sources"'
    assert [s for s in statements if s.refused] == drops


def test_a_multi_line_statement_is_one_statement(tmp_path):
    path = tmp_path / "postgres.log"
    path.write_text(POSTGRES_LOG)
    creates = [s for s in read_postgres_log(path) if s.kind is SCHEMA]
    assert len(creates) == 1


def test_postgres_stamps_every_statement(tmp_path):
    path = tmp_path / "postgres.log"
    path.write_text(POSTGRES_LOG)
    moments = [s.at for s in read_postgres_log(path)]
    assert all(m is not None for m in moments)
    assert moments[0] == datetime(2026, 9, 16, 18, 48, 5, 853000, tzinfo=UTC)


def test_a_missing_log_is_quiet(tmp_path):
    assert list(read_postgres_log(tmp_path / "nope.log")) == []
    assert list(read_mariadb_log(tmp_path / "nope.log")) == []


# -- MariaDB -----------------------------------------------------------

def test_mariadb_statements_are_attributed_through_the_connect_line(tmp_path):
    # The account appears ONCE, on Connect, and everything after is
    # identified only by a numeric connection id.
    path = tmp_path / "queries.log"
    path.write_text(MARIADB_LOG)
    statements = list(read_mariadb_log(path))

    assert [s.account for s in statements] == ["root", "reader", "reader"]
    assert [s.kind for s in statements] == [WRITE, READ, DESTRUCTIVE]


def test_a_stamped_line_does_not_have_its_hour_read_as_a_connection_id(tmp_path):
    # THE first parser bug, and it was silent. MariaDB stamps only the
    # first line of each second; an optional "some non-space" for the
    # stamp let the regex read the HOUR of `260916 18:49:29` as the id,
    # so half of every session was attributed to an id that never
    # connected and surfaced as an account called "unknown".
    path = tmp_path / "queries.log"
    path.write_text(MARIADB_LOG)
    accounts = {s.account for s in read_mariadb_log(path)}
    assert "unknown" not in accounts
    assert accounts == {"root", "reader"}


def test_a_multi_row_insert_is_one_statement(tmp_path):
    path = tmp_path / "queries.log"
    path.write_text(MARIADB_LOG)
    statements = list(read_mariadb_log(path))
    assert len([s for s in statements if s.kind is WRITE]) == 1


def test_the_command_word_does_not_eat_the_statement(tmp_path):
    # Written because a control aimed at the command list did NOT fire,
    # and the list turned out to be load-bearing anyway. `Init DB` and
    # `Close stmt` are two words, so any pattern admitting them also
    # reads `Query INSERT` as a command -- which silently truncates
    # every statement by its first word and reclassifies it.
    path = tmp_path / "queries.log"
    path.write_text(
        "\t\t     4 Connect\treader@localhost on probe using TCP/IP\n"
        "\t\t     4 Init DB\tprobe\n"
        "\t\t     4 Query\tINSERT INTO `t` VALUES (1)\n"
        "\t\t     4 Query\tSELECT 1\n"
    )
    statements = list(read_mariadb_log(path))
    assert [s.text for s in statements] == ["INSERT INTO `t` VALUES (1)", "SELECT 1"]
    assert [s.kind for s in statements] == [WRITE, READ]


def test_a_connection_id_is_forgotten_when_it_quits(tmp_path):
    # Ids are reused. Holding one after Quit would attribute the next
    # session's statements to the previous account.
    path = tmp_path / "queries.log"
    path.write_text(MARIADB_LOG + "\t\t    11 Query\tSELECT 1\n")
    assert [s.account for s in read_mariadb_log(path)][-1] == "unknown"


# -- asking it things --------------------------------------------------

def test_the_summary_counts_by_account_and_kind(tmp_path):
    path = tmp_path / "postgres.log"
    path.write_text(POSTGRES_LOG)
    counts = summarise(list(read_postgres_log(path)))

    assert counts["reader"] == {READ: 1, DESTRUCTIVE: 1, "refused": 1}
    assert counts["writer"] == {WRITE: 1}
    assert counts["sim"] == {SCHEMA: 1}


def test_the_simulators_own_accounts_can_be_set_aside(tmp_path):
    # Every statement the simulation makes is a write, so counting them
    # buries the one line an operator is looking for.
    path = tmp_path / "postgres.log"
    path.write_text(POSTGRES_LOG)
    accounts = {s.account for s in consumers_only(list(read_postgres_log(path)))}
    assert accounts == {"reader", "writer"}
    assert "sim" in OWNERS
