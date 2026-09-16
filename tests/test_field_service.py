"""The field-service pack, run end to end.

The first pack that is a simulation of a business rather than a
feature test: three silos of three different kinds, a job walking a
real lifecycle, money moving between tables, and two scheduled
publications feeding the other two systems.

What is asserted here is mostly CROSS-TABLE and CROSS-SILO: invariants
that only hold if several independently declared events agree with
each other. A single event writing plausible rows would satisfy none
of them.
"""

import json
import pathlib
import urllib.request

import pytest

from simulator import runner
from simulator.relational import fetch_all
from simulator.spec import load_pack

PACK = pathlib.Path(__file__).resolve().parent.parent / "packs" / "field_service.yaml"
#: Three simulated weeks: long enough for jobs to reach `paid`, which
#: needs seven days' dwell after invoicing, without the suite paying
#: for a month of ticks.
SPAN_SECONDS = 21 * 86400


@pytest.fixture(scope="module")
def world(tmp_path_factory, postgres_binaries):
    # Module-scoped: this runs three simulated weeks, and every test
    # below asks a different question of the same world rather than
    # rebuilding it.
    built = runner.build(load_pack(PACK), tmp_path_factory.mktemp("fs"), seed=12)
    runner.seed(built)
    runner.run(built, total_seconds=SPAN_SECONDS, tick_seconds=3600)
    try:
        yield built
    finally:
        runner.stop(built)


def query(world, statement, parameters=()):
    return fetch_all(world.silo("dispatch"), "dispatch", statement, parameters)


def scalar(world, statement, parameters=()):
    return query(world, statement, parameters)[0][0]


# -- the business runs -----------------------------------------------

@pytest.mark.postgres
def test_all_three_silos_are_reachable(world):
    for name in ("dispatch", "books", "payroll"):
        assert world.silo(name).is_reachable(), name


@pytest.mark.postgres
def test_every_silo_says_where_to_connect(world):
    connections = world.connections()
    assert connections["dispatch"].details["database"] == "dispatch"
    assert str(connections["books"].details["base_url"]).startswith("http://127.0.0.1:")
    assert "payroll" in str(connections["payroll"].details["path"])


@pytest.mark.postgres
def test_jobs_walk_the_whole_lifecycle(world):
    statuses = dict(query(world, "SELECT status, count(*) FROM work_orders GROUP BY status"))
    assert statuses.get("paid", 0) > 0, statuses
    assert statuses.get("cancelled", 0) > 0, statuses
    # Terminal states should dominate after three weeks; a machine that
    # never settles would show the working states just as full.
    terminal = statuses.get("paid", 0) + statuses.get("cancelled", 0)
    working = sum(statuses.get(state, 0)
                  for state in ("requested", "quoted", "approved", "completed"))
    assert terminal > working


# -- invariants across tables ----------------------------------------

@pytest.mark.postgres
def test_every_invoice_names_a_real_customer(world):
    # Only reachable because the work order's own row travels with its
    # transition. Before that the customer came out as the literal
    # "unknown", which is how the gap was found.
    assert scalar(world, "SELECT count(*) FROM invoices "
                         "WHERE customer_id NOT IN (SELECT customer_id FROM customers)") == 0
    assert scalar(world, "SELECT count(*) FROM invoices") > 10


@pytest.mark.postgres
def test_an_invoice_is_for_what_was_quoted(world):
    # Two events, declared separately: one sets quoted_total when the
    # job is quoted, another reads it when the job is invoiced.
    mismatched = query(world, """
        SELECT i.invoice_id FROM invoices i
        JOIN work_orders o ON o.work_order_id = i.work_order_id
        WHERE i.total <> o.quoted_total
    """)
    assert mismatched == []


@pytest.mark.postgres
def test_accounts_receivable_reconciles(world):
    # THE invariant worth having. Three independently declared things
    # agree: an effect adding to a balance when a job is invoiced, an
    # effect subtracting when it is paid, and the invoice rows
    # themselves. Any one of them wrong and this fails.
    disagreeing = query(world, """
        SELECT c.customer_id FROM customers c
        LEFT JOIN invoices i ON i.customer_id = c.customer_id
        GROUP BY c.customer_id, c.balance_owed
        HAVING c.balance_owed <> coalesce(sum(i.total) FILTER (WHERE NOT i.paid), 0)
    """)
    assert disagreeing == []
    assert scalar(world, "SELECT sum(balance_owed) FROM customers") > 0


@pytest.mark.postgres
def test_a_cancelled_job_is_never_invoiced(world):
    # The lifecycle makes cancellation reachable only from the early
    # states, so this holds because the state machine says so.
    assert scalar(world, "SELECT count(*) FROM invoices i "
                         "JOIN work_orders o ON o.work_order_id = i.work_order_id "
                         "WHERE o.status = 'cancelled'") == 0


@pytest.mark.postgres
def test_every_completed_job_earned_somebody_something(world):
    # A pay line is written when a job completes, so a job past that
    # point without one means the emission did not fire.
    assert scalar(world, """
        SELECT count(*) FROM work_orders o
        WHERE o.status IN ('completed','invoiced','paid')
        AND o.work_order_id NOT IN (SELECT work_order_id FROM pay_lines)
    """) == 0


@pytest.mark.postgres
def test_timestamps_follow_the_job_through_its_states(world):
    assert scalar(world, "SELECT count(*) FROM work_orders "
                         "WHERE quoted_at IS NOT NULL AND quoted_at < raised_at") == 0
    assert scalar(world, "SELECT count(*) FROM work_orders "
                         "WHERE completed_at IS NOT NULL AND completed_at < quoted_at") == 0


# -- the other two silos ---------------------------------------------

@pytest.mark.postgres
def test_the_bookkeeper_gets_a_file_a_week(world):
    files = world.silo("payroll").listing()
    assert len(files) == 3, files
    assert all(name.startswith("pay_lines_2026-") for name in files)


@pytest.mark.postgres
def test_the_payroll_file_holds_real_pay_lines(world):
    import csv

    name = world.silo("payroll").listing()[-1]
    path = world.silo("payroll").path / name
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["pay_line_id", "technician_id", "work_order_id", "amount", "earned_on"]
    assert len(rows) - 1 == scalar(world, "SELECT count(*) FROM pay_lines")
    technicians = {row[1] for row in rows[1:]}
    assert technicians <= {f"tech_{index:06d}" for index in range(1, 5)}


@pytest.mark.postgres
def test_the_accounting_api_serves_the_invoices(world):
    connection = world.connections()["books"]
    base = str(connection.details["base_url"])
    token = str(connection.details["token"])

    def get(path):
        request = urllib.request.Request(base + path)
        request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    records, path = [], "/v1/invoices"
    while True:
        body = get(path)
        records.extend(body["data"])
        if "cursor" not in body:
            break
        path = f"/v1/invoices?cursor={body['cursor']}"

    assert len(records) == scalar(world, "SELECT count(*) FROM invoices")
    assert isinstance(records[0]["total"], str)
    assert records[0]["issued_at"].endswith("+00:00")


@pytest.mark.postgres
def test_the_api_and_the_database_agree_about_what_is_paid(world):
    # Two silos, no foreign key between them -- they agree because both
    # were told the same thing, which is the kind of join a system
    # reading several silos has to make work.
    connection = world.connections()["books"]
    request = urllib.request.Request(str(connection.details["base_url"]) + "/v1/invoices")
    request.add_header("Authorization", f"Bearer {connection.details['token']}")
    with urllib.request.urlopen(request, timeout=5) as response:
        page = json.loads(response.read())["data"]

    ids = [record["invoice_id"] for record in page]
    placeholders = ", ".join(["%s"] * len(ids))
    in_database = dict(query(
        world, f"SELECT invoice_id, paid FROM invoices WHERE invoice_id IN ({placeholders})",
        ids))
    assert {record["invoice_id"]: record["paid"] for record in page} == in_database


def test_completed_jobs_carry_notes_a_person_would_read(world):
    # Every other column in this pack is an identifier, a number, a
    # date or a short label, so anything reading this business in
    # language had nothing to read at all. Notes are on most real
    # work-order tables and they are where the interesting questions
    # live.
    total, written = fetch_all(
        world.silo("dispatch"), "dispatch",
        "SELECT count(*), count(notes) FROM work_orders")[0]
    assert total > 20
    assert written > 0

    # Only completed jobs have them: a note written before the work
    # happened would be a note about nothing.
    unfinished = fetch_all(
        world.silo("dispatch"), "dispatch",
        "SELECT count(*) FROM work_orders "
        "WHERE completed_at IS NULL AND notes IS NOT NULL")[0][0]
    assert unfinished == 0


def test_the_notes_are_varied_and_in_narrative_order(world):
    notes = [row[0] for row in fetch_all(
        world.silo("dispatch"), "dispatch",
        "SELECT notes FROM work_orders WHERE notes IS NOT NULL")]
    assert len(set(notes)) > 5, "every job wrote the same note"
    assert len({len(note) for note in notes}) > 3, "every note is the same length"

    # Arrive, diagnose, repair, advise -- a subset of that order, never
    # a different one. Prose assembled by picking at random reads as
    # nonsense that happens to be grammatical.
    order = ["Attended site", "Found the fault", "Carried out the repair",
             "Parts used", "Advised the customer"]
    # By where each phrase lands in the note, not by walking `order`
    # and filtering -- that yields indices sorted by construction and
    # would pass against a generator that shuffled.
    for note in notes:
        appearing = [(note.index(phrase), i)
                     for i, phrase in enumerate(order) if phrase in note]
        assert len(appearing) >= 2, note
        by_position = [declared for _, declared in sorted(appearing)]
        assert by_position == sorted(by_position), note
