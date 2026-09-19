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
from datetime import timedelta

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
    # LAST WEEK'S EARNINGS, not every week's. The file used to hold the
    # whole table, so a simulated year rewrote January's pay lines
    # fifty-one times -- which a real weekly export does not do, and
    # which was most of why a year cost two million writes.
    covered = scalar(world, "SELECT count(*) FROM pay_lines WHERE earned_on >= %s",
                     ((world.clock.now() - timedelta(days=7)).date(),))
    assert len(rows) - 1 == covered
    assert covered < scalar(world, "SELECT count(*) FROM pay_lines"), (
        "the window covered everything, so it is not really a window")
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

    # The feed keeps a rolling quarter, so it agrees with the
    # database about that window and not about the whole table -- which
    # is the trap: a tool believing this feed has everything is quietly
    # wrong about anything older.
    recent = scalar(world, "SELECT count(*) FROM invoices "
                           "WHERE issued_at >= %s",
                    (world.clock.now() - timedelta(days=90),))
    assert len(records) == recent
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

    # Only finished jobs have them: a note written before the work
    # happened would be a note about nothing. The archived rows are the
    # exception and say so -- they arrived already finished, from a
    # migration, and carry their own explanation rather than an
    # engineer's.
    unfinished = fetch_all(
        world.silo("dispatch"), "dispatch",
        "SELECT count(*) FROM work_orders WHERE completed_at IS NULL "
        "AND notes IS NOT NULL AND status <> 'archived'")[0][0]
    assert unfinished == 0


def test_the_notes_are_varied_and_in_narrative_order(world):
    # The engineers' notes, not the migrated rows' -- those carry one
    # fixed sentence explaining themselves and would otherwise look
    # like a generator that had stopped varying.
    notes = [row[0] for row in fetch_all(
        world.silo("dispatch"), "dispatch",
        "SELECT notes FROM work_orders WHERE notes IS NOT NULL "
        "AND status <> 'archived'")]
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


# -- who a record belongs to ------------------------------------------

def test_customers_are_partitioned_by_branch(world):
    # Every business partitions its records somehow, and a consumer
    # with access control needs something in the data to enforce
    # against. With no such column every user of a connected tool sees
    # everything and nothing can be denied.
    counts = dict(fetch_all(world.silo("dispatch"), "dispatch",
                            "SELECT branch, count(*) FROM customers GROUP BY branch"))
    assert set(counts) == {"bristol", "bath", "weston"}
    # Meaningfully sized groups. A boundary that puts everybody on one
    # side is not a boundary.
    assert min(counts.values()) >= 2, counts


def test_the_account_type_follows_a_rule_the_row_explains(world):
    # The difference between a classification and a label. Trade
    # accounts are opened where a firm expects volume, and here that is
    # the busiest branch -- so a trade account anywhere else would mean
    # the rule was not applied.
    elsewhere = fetch_all(
        world.silo("dispatch"), "dispatch",
        "SELECT count(*) FROM customers WHERE account_type = 'trade' "
        "AND branch <> 'bristol'")[0][0]
    assert elsewhere == 0

    both = fetch_all(world.silo("dispatch"), "dispatch",
                     "SELECT count(DISTINCT account_type) FROM customers")[0][0]
    assert both == 2, "the rule chose the same branch every time"


def test_a_job_inherits_its_branch_through_its_customer(world):
    # A work order has no branch of its own; it belongs to the customer
    # that does. That indirection is how most real records are
    # classified.
    rows = fetch_all(world.silo("dispatch"), "dispatch", """
        SELECT c.branch, count(*) FROM work_orders w
        JOIN customers c ON c.customer_id = w.customer_id
        GROUP BY c.branch
    """)
    assert len(rows) == 3
    assert all(count > 0 for _, count in rows)


def test_some_jobs_have_no_customer_to_inherit_from(world):
    # DELIBERATE, and the point of it. A boundary that cannot be
    # resolved is a different thing from one that denies you: the first
    # is a data-integrity signal, the second a permission outcome, and
    # a careful consumer has to tell them apart. Until a row like this
    # existed the first case was unreachable.
    orphans = fetch_all(world.silo("dispatch"), "dispatch", """
        SELECT count(*) FROM work_orders w WHERE NOT EXISTS
        (SELECT 1 FROM customers c WHERE c.customer_id = w.customer_id)
    """)[0][0]
    assert orphans == 6

    # And they are a small minority, so a consumer that drops them
    # silently still looks like it is working -- which is exactly why
    # the case is worth having.
    total = fetch_all(world.silo("dispatch"), "dispatch",
                      "SELECT count(*) FROM work_orders")[0][0]
    assert orphans < total / 10, (orphans, total)


def test_an_orphan_says_why_it_is_one(world):
    # A row that looks like corruption is less useful than one that
    # explains itself: somebody reading this table should be able to
    # see that the customer record did not survive a migration.
    notes = [row[0] for row in fetch_all(
        world.silo("dispatch"), "dispatch",
        "SELECT notes FROM work_orders w WHERE NOT EXISTS "
        "(SELECT 1 FROM customers c WHERE c.customer_id = w.customer_id)")]
    assert all("Migrated from the old system" in note for note in notes), notes


# -- when things changed, and what stopped being true -----------------

def test_customers_record_when_they_were_made_and_by_whom(world):
    # On almost every table in almost every business schema, and on
    # none of ours until now.
    rows = fetch_all(world.silo("dispatch"), "dispatch",
                     "SELECT created_by, count(*) FROM customers GROUP BY created_by")
    assert len(rows) > 1, "every customer was created the same way"
    assert {str(who) for who, _ in rows} <= {"reception", "import", "engineer"}

    missing = fetch_all(world.silo("dispatch"), "dispatch",
                        "SELECT count(*) FROM customers "
                        "WHERE created_at IS NULL OR updated_at IS NULL")[0][0]
    assert missing == 0


def test_updated_at_moves_when_the_row_does(world):
    # A column that only records the FIRST change is worse than no
    # column: a consumer syncing "everything since yesterday" would
    # silently miss every row that has ever been touched twice.
    changed, total = fetch_all(
        world.silo("dispatch"), "dispatch",
        "SELECT count(*) FILTER (WHERE updated_at > created_at), count(*) "
        "FROM customers")[0]
    assert changed > 0
    assert changed <= total

    # And it never moves backwards, which is what makes "since" mean
    # anything.
    backwards = fetch_all(world.silo("dispatch"), "dispatch",
                          "SELECT count(*) FROM customers "
                          "WHERE updated_at < created_at")[0][0]
    assert backwards == 0


def test_an_incremental_sync_can_be_keyed_off_updated_at(world):
    # The reason the column exists. "Give me everything that changed
    # since I last asked" has to return a strict subset, or a consumer
    # paging through changes either loops or misses rows.
    midpoint = fetch_all(
        world.silo("dispatch"), "dispatch",
        "SELECT min(updated_at) + (max(updated_at) - min(updated_at)) / 2 "
        "FROM customers")[0][0]
    since, total = fetch_all(
        world.silo("dispatch"), "dispatch",
        "SELECT count(*) FILTER (WHERE updated_at > %s), count(*) FROM customers",
        (midpoint,))[0]
    assert 0 < since < total, (since, total)


def test_voided_invoices_are_marked_not_removed(world):
    # Real systems mark a record dead rather than removing it, because
    # the row is evidence even after it stops being true.
    total, void = fetch_all(
        world.silo("dispatch"), "dispatch",
        "SELECT count(*), count(*) FILTER (WHERE is_void) FROM invoices")[0]
    assert void > 0, "nothing was ever voided"
    assert void < total / 3, (void, total)

    # Every voided row says when, and no live row pretends to have been.
    inconsistent = fetch_all(
        world.silo("dispatch"), "dispatch",
        "SELECT count(*) FROM invoices "
        "WHERE (is_void AND voided_at IS NULL) OR (NOT is_void AND voided_at IS NOT NULL)"
    )[0][0]
    assert inconsistent == 0


def test_forgetting_the_void_filter_overstates_revenue_quietly(world):
    # THE reason a soft delete is worth simulating. A consumer that
    # forgets `WHERE NOT is_void` is wrong silently, plausibly, and by
    # a small enough margin that nobody checks.
    real, naive = fetch_all(
        world.silo("dispatch"), "dispatch",
        "SELECT sum(total) FILTER (WHERE NOT is_void), sum(total) FROM invoices")[0]
    assert naive > real, "voiding changed nothing, so the trap is not reachable"
    # Big enough to matter, small enough to miss.
    overstatement = (naive - real) / real
    assert 0.005 < overstatement < 0.5, overstatement


# -- a relationship that is not a foreign key -------------------------

def test_engineers_hold_skills_through_a_join_table(world):
    # Every relationship in this pack was a foreign-key column until
    # now, so the join-table shape -- which any ontology models
    # differently and any consumer resolves differently -- was entirely
    # unexercised.
    skills, techs, pairs = fetch_all(world.silo("dispatch"), "dispatch", """
        SELECT (SELECT count(*) FROM skills),
               (SELECT count(*) FROM technicians),
               (SELECT count(*) FROM technician_skills)
    """)[0]
    assert skills > 1 and techs > 1
    # Two each: `count` alongside `per` means rows PER SUBJECT, which
    # was refused before and is what made a join table inexpressible.
    assert pairs == techs * 2


def test_every_engineer_has_more_than_one_row(world):
    # The property a join table exists for. One row per subject would
    # have been a foreign key with extra steps.
    rows = fetch_all(world.silo("dispatch"), "dispatch",
                     "SELECT technician_id, count(*) FROM technician_skills "
                     "GROUP BY technician_id")
    assert rows
    assert all(count == 2 for _, count in rows), rows


def test_no_engineer_holds_the_same_ticket_twice(world):
    # Picks used to be independent per row, so with six skills and two
    # draws a collision was likely somewhere among four engineers and
    # somebody ended up "qualified in Gas Safe and Gas Safe". That is a
    # simulator artefact, not something a firm's records would say.
    #
    # THE PROMISE COMES FROM THE PACK, NOT THE DATABASE. This schema
    # has no composite primary key, so nothing stops a duplicate pair
    # being written -- the seed step asks for distinct picks instead,
    # and a consumer should not assume the property holds for rows it
    # did not see written.
    pairs, distinct = fetch_all(
        world.silo("dispatch"), "dispatch",
        "SELECT count(*), count(DISTINCT (technician_id, skill_id)) "
        "FROM technician_skills")[0]
    assert pairs == distinct, f"{pairs - distinct} engineers hold a ticket twice"

    nulls = fetch_all(world.silo("dispatch"), "dispatch",
                      "SELECT count(*) FROM technician_skills "
                      "WHERE technician_id IS NULL OR skill_id IS NULL")[0][0]
    assert nulls == 0


def test_neither_end_of_a_pair_is_missing(world):
    orphans = fetch_all(world.silo("dispatch"), "dispatch", """
        SELECT count(*) FROM technician_skills ts
        WHERE NOT EXISTS (SELECT 1 FROM technicians t
                          WHERE t.technician_id = ts.technician_id)
           OR NOT EXISTS (SELECT 1 FROM skills s WHERE s.skill_id = ts.skill_id)
    """)[0][0]
    assert orphans == 0


def test_reference_rows_do_not_share_a_name(world):
    # A defect this found, and it predates the join table: `choice`
    # draws WITH replacement, so four draws from four names left three
    # engineers called Okafor and six draws from six skills produced
    # two both called "Solar". Reference data that repeats itself looks
    # corrupt rather than generated.
    for table, column in (("technicians", "name"), ("skills", "name")):
        total, distinct = fetch_all(
            world.silo("dispatch"), "dispatch",
            f"SELECT count(*), count(DISTINCT {column}) FROM {table}")[0]
        assert total == distinct, f"{table}.{column}: {distinct} names for {total} rows"


def test_a_dispute_voids_one_bill_and_not_a_history(world):
    # A YEAR-SCALE FINDING, reproduced small. Voiding used to fire per
    # customer and update every invoice its `where` matched -- which is
    # all of them -- so each dispute re-voided that customer's whole
    # history. Over twenty-five days that looked like 6% voided and
    # plausible; over a year it was 56% of the first quarter, with
    # invoices voided eleven months after they were issued.
    #
    # Keyed on the work order now, so a void belongs to the job it
    # disputes.
    voided = fetch_all(world.silo("dispatch"), "dispatch",
                       "SELECT work_order_id, count(*) FROM invoices "
                       "WHERE is_void GROUP BY work_order_id")
    assert voided, "nothing was disputed"
    assert all(count == 1 for _, count in voided), voided

    # And every voided invoice belongs to a job that really was
    # disputed, rather than to one that merely shares a customer.
    mismatched = fetch_all(world.silo("dispatch"), "dispatch", """
        SELECT count(*) FROM invoices i
        JOIN work_orders w ON w.work_order_id = i.work_order_id
        WHERE i.is_void AND w.status <> 'disputed'
    """)[0][0]
    assert mismatched == 0


def test_a_dispute_is_an_outcome_a_job_can_reach(world):
    statuses = dict(fetch_all(world.silo("dispatch"), "dispatch",
                              "SELECT status, count(*) FROM work_orders GROUP BY status"))
    assert statuses.get("disputed", 0) > 0
    # Rare, as a dispute should be: matching the dwell of payment makes
    # the share close to the ratio of the rates, and a shorter one gave
    # disputes a four-day head start that pushed them to a fifth of all
    # bills.
    assert statuses["disputed"] < sum(statuses.values()) / 5


# -- the same household, twice ----------------------------------------

def test_some_households_appear_twice(world):
    # Reception took a call, could not find the customer, and made a
    # new record. It happens in every firm with a search box and a
    # hurry, and it is the most common thing wrong with a small
    # business's customer table.
    duplicated = fetch_all(world.silo("dispatch"), "dispatch",
                           "SELECT name, count(*) FROM customers "
                           "GROUP BY name HAVING count(*) > 1")
    assert duplicated, "no household appears twice"
    assert len(duplicated) < 5, "half the table is duplicates, which is not a firm"


def test_a_duplicate_agrees_with_itself(world):
    # Name AND phone, from the same record. Two independent draws would
    # give one household's name against another's number -- a different
    # defect, and not the one being declared.
    mismatched = fetch_all(world.silo("dispatch"), "dispatch", """
        SELECT count(*) FROM customers a JOIN customers b
          ON a.name = b.name AND a.customer_id <> b.customer_id
        WHERE a.phone IS DISTINCT FROM b.phone
    """)[0][0]
    assert mismatched == 0


def test_both_copies_of_a_household_accumulate_work(world):
    # What makes a duplicate expensive rather than untidy: jobs, bills
    # and balances land against whichever record reception happened to
    # find, so neither is the whole story.
    #
    # This was ZERO for every duplicate until the subject cache was
    # fixed -- a seed step that picked from the customer table filled
    # the cache with the rows existing at that moment, so every event
    # afterwards was `per` a stale list and the duplicates never raised
    # a job.
    rows = fetch_all(world.silo("dispatch"), "dispatch", """
        SELECT c.customer_id, count(w.work_order_id)
        FROM customers c LEFT JOIN work_orders w ON w.customer_id = c.customer_id
        WHERE c.name IN (SELECT name FROM customers GROUP BY name HAVING count(*) > 1)
        GROUP BY c.customer_id
    """)
    assert len(rows) >= 4
    assert all(count > 0 for _, count in rows), rows


# -- where the systems disagree with each other -----------------------

def test_a_dispute_claws_back_the_engineers_commission(world):
    # The job was disputed, so the commission goes back. Keyed on the
    # work order, so it is that job's pay line and not the engineer's
    # whole week.
    clawed = query(world, """
        SELECT count(*) FROM pay_lines p
        JOIN work_orders w ON w.work_order_id = p.work_order_id
        WHERE w.status = 'disputed' AND p.amount = 0
    """)[0][0]
    disputed = scalar(world, "SELECT count(*) FROM work_orders "
                             "WHERE status = 'disputed'")
    assert disputed > 0
    assert clawed > 0

    # And nothing else was zeroed: an engineer who was not disputed
    # still gets paid.
    wrongly = query(world, """
        SELECT count(*) FROM pay_lines p
        JOIN work_orders w ON w.work_order_id = p.work_order_id
        WHERE w.status <> 'disputed' AND p.amount = 0
    """)[0][0]
    assert wrongly == 0


def test_the_payroll_files_and_dispatch_disagree_about_what_was_earned(world):
    # THE thing only a multi-silo simulator can produce. A dispute
    # lands at least a week after the job was invoiced and payroll goes
    # out weekly, so by the time the commission is clawed back the CSV
    # covering that week has already been written and sent.
    #
    # Neither source is lying. The file is what was true when it was
    # produced, and Dispatch is what is true now. Reconciling them is
    # somebody's Monday morning, and until this existed nothing here
    # gave anyone that problem.
    import csv
    from decimal import Decimal

    live = dict(query(world, "SELECT pay_line_id, amount FROM pay_lines"))
    exported: dict[str, Decimal] = {}
    for path in sorted(world.silo("payroll").path.glob("*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                exported[row["pay_line_id"]] = Decimal(row["amount"])

    assert exported, "nothing has been exported yet"
    disagreeing = [key for key, amount in exported.items()
                   if key in live and amount != live[key]]
    assert disagreeing, "the files and the database agree about everything"
    # A minority, so a consumer that trusts either alone still looks
    # like it is working.
    assert len(disagreeing) < len(exported) / 4, (len(disagreeing), len(exported))

    # And the disagreement always runs one way: the file paid out more
    # than Dispatch now says was earned, never less.
    for key in disagreeing:
        assert exported[key] > live[key], (key, exported[key], live[key])


def test_the_skills_are_the_ones_a_plumbing_firm_really_has(world):
    # Written out rather than generated. A lookup table is the one
    # place where a name drawn at random is simply wrong -- this used
    # to produce "Skill skill_000001", because `choice` draws WITH
    # replacement and six draws from six options gave two called the
    # same and none called several of the others.
    names = {row[0] for row in query(world, "SELECT name FROM skills")}
    assert names == {"Gas Safe", "Unvented hot water", "Oil-fired",
                     "Commercial", "Legionella", "Solar thermal"}

    ids = {row[0] for row in query(world, "SELECT skill_id FROM skills")}
    assert all(not identifier.startswith("skill_0") for identifier in ids), ids
