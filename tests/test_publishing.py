"""Tests for periodic events and publishing files.

The integration small businesses actually have: not a database
connection, a folder somebody drops a CSV into on a schedule. Tested
together because neither is much use alone -- a periodic event with
nothing to write does nothing, and a publication with no occasion to
run never fires.
"""

import csv
import textwrap

import pytest

from simulator import runner
from simulator.spec import PackError, load_pack, load_spec

EXPORT = textwrap.dedent("""
    pack: nightly_export

    silos:
      ops:  {kind: postgresql, database: ops}
      bank: {kind: filedrop}

    schemas:
      ops:
        tables:
          invoices:
            columns:
              invoice_id: {type: text, length: 64, primary_key: true, nullable: false}
              customer:   {type: text, length: 120, nullable: false}
              total:      {type: decimal, precision: 19, scale: 4, nullable: false}
              internal_note: {type: text, length: 200}

    seed:
      - table: ops.invoices
        count: 5
        columns:
          invoice_id: {generator: id, prefix: inv}
          customer:   {generator: choice, options: [Okafor, "Café Solstråle 🌞"]}
          total:      {generator: decimal, min: 10, max: 900, scale: 4}
          internal_note: {generator: constant, value: "not for the bank"}

    events:
      nightly_export:
        every: 1d
        emits:
          - publish: bank
            filename: {generator: template, pattern: "invoices_{today}.csv"}
            rows_from: ops.invoices
            columns: [invoice_id, customer, total]
    """)


def write_pack(tmp_path, source=EXPORT, name="export"):
    path = tmp_path / f"{name}.yaml"
    path.write_text(source)
    return load_pack(path)


@pytest.fixture
def world(tmp_path, postgres_binaries):
    built = runner.build(write_pack(tmp_path), tmp_path / "var", seed=3)
    runner.seed(built)
    try:
        yield built
    finally:
        runner.stop(built)


def published(world):
    return world.silo("bank").listing()


def read_text(world, name):
    return (world.silo("bank").path / name).read_text(encoding="utf-8-sig")


def read_csv(world, name):
    path = world.silo("bank").path / name
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


# -- publishing ------------------------------------------------------

@pytest.mark.postgres
def test_a_file_appears_once_a_day(world):
    runner.run(world, total_seconds=4 * 86400, tick_seconds=3600)
    assert len(published(world)) == 4


@pytest.mark.postgres
def test_files_are_named_after_the_day_they_cover(world):
    runner.run(world, total_seconds=3 * 86400, tick_seconds=3600)
    names = published(world)
    assert names == sorted(names), "dates should sort naturally"
    assert all(name.startswith("invoices_2026-01-") for name in names), names
    assert len(set(names)) == len(names), "a day was exported twice"


@pytest.mark.postgres
def test_the_file_holds_the_declared_columns_and_no_others(world):
    # The source table carries `internal_note`, which the export does
    # not list. A first version of this test had the table and the
    # export agreeing exactly, so it passed against an implementation
    # selecting every column -- an export that silently widens when the
    # table does is the failure this guards against, and it needs a
    # column to leave out before it can be seen.
    runner.run(world, total_seconds=86400, tick_seconds=3600)
    rows = read_csv(world, published(world)[0])
    assert rows[0] == ["invoice_id", "customer", "total"]
    assert all(len(row) == 3 for row in rows)
    assert "not for the bank" not in read_text(world, published(world)[0])
    assert len(rows) == 6  # a header and five invoices


@pytest.mark.postgres
def test_the_export_survives_the_round_trip_intact(world):
    # Read back the way a consumer would, with the BOM handled. The
    # emoji is the interesting part: it has to survive PostgreSQL, the
    # CSV writer and utf-8-sig decoding.
    runner.run(world, total_seconds=86400, tick_seconds=3600)
    rows = read_csv(world, published(world)[0])
    customers = {row[1] for row in rows[1:]}
    assert customers <= {"Okafor", "Café Solstråle 🌞"}
    assert all(row[0].startswith("inv_") for row in rows[1:])


@pytest.mark.postgres
def test_money_keeps_its_scale_in_the_file(world):
    # A CSV is text, so a total written as 73.8313 must arrive that way
    # rather than as a float's idea of it.
    runner.run(world, total_seconds=86400, tick_seconds=3600)
    rows = read_csv(world, published(world)[0])
    assert all(len(row[2].split(".")[1]) == 4 for row in rows[1:]), rows


@pytest.mark.postgres
def test_nothing_partial_is_ever_left_behind(world):
    # The silo publishes atomically; this checks nothing escaped that.
    runner.run(world, total_seconds=2 * 86400, tick_seconds=3600)
    assert not list(world.silo("bank").path.glob("*.part"))


# -- how often a periodic event fires --------------------------------

@pytest.mark.postgres
def test_a_periodic_event_does_not_care_about_tick_size(tmp_path, postgres_binaries):
    # It counts interval boundaries crossed rather than remembering
    # when it last fired, so slicing time differently cannot change how
    # many times it happens.
    def files_at(tick_seconds, name):
        built = runner.build(write_pack(tmp_path), tmp_path / name, seed=3)
        try:
            runner.seed(built)
            runner.run(built, total_seconds=5 * 86400, tick_seconds=tick_seconds)
            return len(built.silo("bank").listing())
        finally:
            runner.stop(built)

    assert files_at(900, "fine") == files_at(21600, "coarse") == 5


@pytest.mark.postgres
def test_an_interval_longer_than_the_run_never_fires(tmp_path, postgres_binaries):
    source = EXPORT.replace("every: 1d", "every: 30d")
    built = runner.build(write_pack(tmp_path, source, "monthly"), tmp_path / "m", seed=3)
    try:
        runner.seed(built)
        runner.run(built, total_seconds=3 * 86400, tick_seconds=3600)
        assert built.silo("bank").listing() == []
    finally:
        runner.stop(built)


@pytest.mark.postgres
def test_a_tick_longer_than_the_interval_fires_for_each_one_crossed(tmp_path,
                                                                    postgres_binaries):
    # A single three-day tick crosses three daily boundaries. Firing
    # once would silently lose two days of exports.
    built = runner.build(write_pack(tmp_path), tmp_path / "jump", seed=3)
    try:
        runner.seed(built)
        runner.run(built, total_seconds=3 * 86400, tick_seconds=3 * 86400)
        assert len(built.silo("bank").listing()) == 3
    finally:
        runner.stop(built)


# -- declaration, checked at load ------------------------------------

def base(event) -> dict:
    return {
        "pack": "x",
        "silos": {"ops": {"kind": "postgresql", "database": "ops"},
                  "bank": {"kind": "filedrop"},
                  "api": {"kind": "rest"}},
        "schemas": {"ops": {"tables": {"invoices": {"columns": {
            "invoice_id": {"type": "text", "length": 64, "primary_key": True,
                           "nullable": False},
            "total": {"type": "decimal", "precision": 19, "scale": 4}}}}}},
        "events": {"e": event},
    }


def publication(**changes) -> dict:
    return {"publish": "bank",
            "filename": {"generator": "template", "pattern": "x_{today}.csv"},
            "rows_from": "ops.invoices",
            "columns": ["invoice_id"], **changes}


def test_a_periodic_publication_loads():
    pack = load_spec(base({"every": "1d", "emits": [publication()]}))
    assert pack.events[0].trigger.every_seconds == 86400


def test_an_event_fires_exactly_one_way():
    with pytest.raises(PackError, match="an event fires one way"):
        load_spec(base({"every": "1d", "rate_per_hour": 1.0, "emits": [publication()]}))


def test_publishing_to_a_database_silo_is_refused():
    # A folder is not a table and a table is not a folder.
    with pytest.raises(PackError, match="needs a filedrop silo"):
        load_spec(base({"every": "1d", "emits": [publication(publish="ops")]}))


def test_publishing_to_an_api_silo_is_refused():
    with pytest.raises(PackError, match="needs a filedrop silo"):
        load_spec(base({"every": "1d", "emits": [publication(publish="api")]}))


def test_an_export_must_list_its_columns():
    # Declared rather than `all`, because an export is a contract with
    # whoever reads it and should not silently gain a column when the
    # table does.
    with pytest.raises(PackError, match="must list the columns"):
        load_spec(base({"every": "1d", "emits": [
            {"publish": "bank",
             "filename": {"generator": "template", "pattern": "x.csv"},
             "rows_from": "ops.invoices"}]}))


def test_exporting_a_column_that_does_not_exist_is_refused():
    with pytest.raises(PackError, match=r"no column\(s\) \['vat'\]"):
        load_spec(base({"every": "1d", "emits": [publication(columns=["vat"])]}))


def test_a_filename_referring_to_something_that_is_not_offered_is_refused():
    with pytest.raises(PackError, match="refers to 'yesterday'"):
        load_spec(base({"every": "1d", "emits": [publication(
            filename={"generator": "template", "pattern": "x_{yesterday}.csv"})]}))


def test_a_filename_may_refer_to_the_facts_a_publication_offers():
    for fact in ("today", "now"):
        load_spec(base({"every": "1d", "emits": [publication(
            filename={"generator": "template", "pattern": f"x_{{{fact}}}.csv"})]}))


def test_a_publication_needs_a_filename():
    with pytest.raises(PackError, match="needs a `filename`"):
        load_spec(base({"every": "1d", "emits": [
            {"publish": "bank", "rows_from": "ops.invoices",
             "columns": ["invoice_id"]}]}))


def test_a_malformed_interval_is_refused():
    with pytest.raises(PackError, match="not a duration"):
        load_spec(base({"every": "nightly", "emits": [publication()]}))


def test_rows_from_a_table_that_does_not_exist_is_refused():
    with pytest.raises(PackError, match="has no table 'ledger'"):
        load_spec(base({"every": "1d", "emits": [publication(rows_from="ops.ledger")]}))
