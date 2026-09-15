"""Tests for exposing a collection through a REST silo.

The other half of how a small business is reached. A file drop is a
folder somebody writes into; an API is a collection somebody polls,
and what is worth pinning is the conversion between them -- a database
row is full of types JSON has no way to carry.
"""

import json
import textwrap
import urllib.request

import pytest

from simulator import runner
from simulator.spec import PackError, load_pack, load_spec

API = textwrap.dedent("""
    pack: books_api

    silos:
      ops:   {kind: postgresql, database: ops}
      books: {kind: rest, options: {page_size: 10}}

    schemas:
      ops:
        tables:
          invoices:
            columns:
              invoice_id: {type: text, length: 64, primary_key: true, nullable: false}
              customer:   {type: text, length: 120, nullable: false}
              total:      {type: decimal, precision: 19, scale: 4, nullable: false}
              issued_at:  {type: timestamp, nullable: false}
              paid:       {type: boolean, nullable: false}
              note:       {type: text, length: 120}
              internal:   {type: text, length: 120}

    seed:
      - table: ops.invoices
        count: 25
        columns:
          invoice_id: {generator: id, prefix: inv}
          customer:   {generator: choice, options: [Okafor, "Café Solstråle 🌞"]}
          total:      {generator: decimal, min: 10, max: 900, scale: 4}
          issued_at:  {generator: now}
          paid:       {generator: weighted, options: {true: 1, false: 3}}
          internal:   {generator: constant, value: "not for the API"}

    events:
      refresh_books:
        every: 6h
        emits:
          - expose: books
            collection: invoices
            rows_from: ops.invoices
            columns: [invoice_id, customer, total, issued_at, paid, note]
    """)


def write_pack(tmp_path, source=API, name="api"):
    path = tmp_path / f"{name}.yaml"
    path.write_text(source)
    return load_pack(path)


@pytest.fixture
def world(tmp_path, postgres_binaries):
    built = runner.build(write_pack(tmp_path), tmp_path / "var", seed=5)
    runner.seed(built)
    runner.run(built, total_seconds=86400, tick_seconds=3600)
    try:
        yield built
    finally:
        runner.stop(built)


def get(world, path):
    """One request, the way a consumer would make it."""
    connection = world.connections()["books"]
    request = urllib.request.Request(str(connection.details["base_url"]) + path)
    request.add_header("Authorization", f"Bearer {connection.details['token']}")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def drain(world, collection):
    """Follow the cursor to the end, as a correct consumer must."""
    records, path = [], f"/v1/{collection}"
    while True:
        body = get(world, path)
        records.extend(body["data"])
        if "cursor" not in body:
            return records
        path = f"/v1/{collection}?cursor={body['cursor']}"


# -- the collection --------------------------------------------------

@pytest.mark.postgres
def test_the_collection_is_served_over_http(world):
    assert len(drain(world, "invoices")) == 25


@pytest.mark.postgres
def test_it_paginates_like_a_real_api(world):
    # The silo's own page size, declared as a silo option. A consumer
    # reading only the first page must visibly lose data -- which is
    # the most common integration bug against APIs of this shape.
    body = get(world, "/v1/invoices")
    assert len(body["data"]) == 10
    assert "cursor" in body
    assert len(drain(world, "invoices")) == 25


@pytest.mark.postgres
def test_money_is_a_string_not_a_json_number(world):
    # JSON has no decimal type, and a float cannot represent 0.10 --
    # sending money as a number would reintroduce, one layer up, the
    # error the schema layer refuses to allow in a column. Real APIs
    # send a string or integer minor units, never a JSON number.
    record = drain(world, "invoices")[0]
    assert isinstance(record["total"], str)
    assert len(record["total"].split(".")[1]) == 4


@pytest.mark.postgres
def test_timestamps_are_iso_strings_with_an_offset(world):
    record = drain(world, "invoices")[0]
    assert isinstance(record["issued_at"], str)
    assert record["issued_at"].startswith("2026-01-01T")
    assert record["issued_at"].endswith("+00:00")


@pytest.mark.postgres
def test_types_json_can_carry_pass_through_untouched(world):
    # A null must stay null rather than becoming the string "None",
    # and a boolean must stay a boolean.
    records = drain(world, "invoices")
    assert all(record["note"] is None for record in records)
    assert {type(record["paid"]) for record in records} == {bool}
    assert all(isinstance(record["customer"], str) for record in records)


@pytest.mark.postgres
def test_the_emoji_survives_the_whole_path(world):
    # PostgreSQL, the JSON encoder, HTTP, and back.
    customers = {record["customer"] for record in drain(world, "invoices")}
    assert customers <= {"Okafor", "Café Solstråle 🌞"}


@pytest.mark.postgres
def test_only_the_declared_columns_are_exposed(world):
    # The table carries `internal`, which the pack does not list.
    record = drain(world, "invoices")[0]
    assert set(record) == {"invoice_id", "customer", "total", "issued_at", "paid", "note"}
    assert "not for the API" not in json.dumps(drain(world, "invoices"))


@pytest.mark.postgres
def test_a_refresh_replaces_rather_than_appends(world):
    # Asking an API for invoices returns the invoices, not a new batch
    # each time. The world above ran four six-hour periods, so an
    # appending implementation would show a hundred records.
    assert len(drain(world, "invoices")) == 25


@pytest.mark.postgres
def test_an_unknown_collection_is_still_404(world):
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(world, "/v1/receipts")
    assert raised.value.code == 404


# -- declaration, checked at load ------------------------------------

def base(emission) -> dict:
    return {
        "pack": "x",
        "silos": {"ops": {"kind": "postgresql", "database": "ops"},
                  "books": {"kind": "rest"},
                  "drop": {"kind": "filedrop"}},
        "schemas": {"ops": {"tables": {"invoices": {"columns": {
            "invoice_id": {"type": "text", "length": 64, "primary_key": True,
                           "nullable": False}}}}}},
        "events": {"e": {"every": "1d", "emits": [emission]}},
    }


def exposure(**changes) -> dict:
    return {"expose": "books", "collection": "invoices",
            "rows_from": "ops.invoices", "columns": ["invoice_id"], **changes}


def test_an_exposure_loads():
    pack = load_spec(base(exposure()))
    assert pack.events[0].emissions[0].collection == "invoices"


def test_exposing_through_a_file_drop_is_refused():
    # A collection is not a document.
    with pytest.raises(PackError, match="needs a rest silo"):
        load_spec(base(exposure(expose="drop")))


def test_exposing_through_a_database_is_refused():
    with pytest.raises(PackError, match="needs a rest silo"):
        load_spec(base(exposure(expose="ops")))


@pytest.mark.parametrize("name", ["in voices", "in/voices", "in-voices", "in?v"])
def test_a_collection_name_must_survive_being_a_url_path(name):
    with pytest.raises(PackError, match="URL path segment"):
        load_spec(base(exposure(collection=name)))


def test_an_exposure_must_list_its_columns():
    with pytest.raises(PackError, match="must list the columns"):
        load_spec(base({"expose": "books", "collection": "invoices",
                        "rows_from": "ops.invoices"}))


def test_exposing_a_column_that_does_not_exist_is_refused():
    with pytest.raises(PackError, match=r"no column\(s\) \['vat'\]"):
        load_spec(base(exposure(columns=["vat"])))


def test_rows_from_a_table_that_does_not_exist_is_refused():
    with pytest.raises(PackError, match="has no table 'ledger'"):
        load_spec(base(exposure(rows_from="ops.ledger")))


def test_an_unrecognised_key_is_refused():
    with pytest.raises(PackError, match="does not understand"):
        load_spec(base(exposure(filename={"generator": "now"})))
