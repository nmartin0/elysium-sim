"""Roadmap 14: reading the silos that are not databases.

Nothing here imports `simulator`. The folder is read as a folder and
the API over HTTP, the way anything else would.
"""

import csv
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest


def published(details):
    return sorted(path for path in Path(details["path"]).glob("*.csv"))


def get(details, path, token=...):
    request = urllib.request.Request(str(details["base_url"]) + path)
    if token is not ...:
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
    else:
        request.add_header("Authorization", f"Bearer {details['token']}")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


# -- 14a and 14b: the folder -------------------------------------------

def test_the_folder_holds_one_complete_file_per_day(connections):
    # One file per simulated day, each named after the day it covers.
    # A first version only asserted that SOME file existed, which one
    # file overwritten daily also satisfies -- so it passed against a
    # publisher that reused a single name and lost every earlier day.
    files = published(connections["drop"])
    assert len(files) > 1, f"expected a file per day, got {[f.name for f in files]}"
    names = [path.name for path in files]
    assert len(set(names)) == len(names), names
    assert all(name.startswith("readings_2026-") for name in names), names
    # Nothing half-written: the silo renames into place, so a consumer
    # polling the folder sees a file either absent or complete.
    assert not list(Path(connections["drop"]["path"]).glob("*.part"))


def test_each_days_file_is_named_after_that_day(connections):
    names = [path.name for path in published(connections["drop"])]
    # Dates sort naturally, which is what makes "latest file" a
    # meaningful thing for a consumer to ask for.
    assert names == sorted(names)


def test_the_encoding_is_declared_and_correct(connections):
    # These files carry a BOM because Excel needs one. A consumer
    # reading plain utf-8 gets three stray bytes on its first field
    # name, so the descriptor says which encoding to use.
    details = connections["drop"]
    assert details["encoding"] == "utf-8-sig"

    raw = published(details)[0].read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")


def test_reading_it_the_declared_way_gives_clean_field_names(connections):
    details = connections["drop"]
    with published(details)[0].open("r", encoding=details["encoding"], newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["reading_id", "label", "amount", "settled", "taken_at",
                       "quantity"]


def test_reading_it_as_plain_utf8_visibly_breaks(connections):
    # Pinned deliberately. This is the condition a real consumer meets,
    # so the simulator must keep producing it rather than quietly
    # tidying it away.
    details = connections["drop"]
    with published(details)[0].open("r", encoding="utf-8", newline="") as handle:
        first = next(csv.reader(handle))
    assert first[0] != "reading_id"
    assert first[0].endswith("reading_id")


def test_unicode_survives_the_file(connections):
    details = connections["drop"]
    text = published(details)[0].read_text(encoding=details["encoding"])
    assert "Café Solstråle 🌞" in text


def test_money_in_the_file_keeps_its_scale(connections):
    details = connections["drop"]
    with published(details)[0].open("r", encoding=details["encoding"], newline="") as handle:
        rows = list(csv.reader(handle))
    amounts = [row[2] for row in rows[1:]]
    assert amounts
    # Text, so it must carry the scale rather than a float's idea of it.
    assert all(len(amount.split(".")[1]) == 4 for amount in amounts), amounts[:3]


# -- 14c: following the cursor -----------------------------------------

def test_reading_only_the_first_page_silently_loses_data(connections):
    # The most common integration bug against APIs of this shape, and
    # it is silent: the data looks fine, there is just less of it.
    details = connections["api"]
    first = get(details, "/v1/readings")
    assert "cursor" in first, "page size too high to demonstrate the trap"

    everything, path = [], "/v1/readings"
    while True:
        body = get(details, path)
        everything.extend(body["data"])
        if "cursor" not in body:
            break
        path = f"/v1/readings?cursor={body['cursor']}"

    assert len(everything) > len(first["data"])


def test_the_api_and_the_database_agree(connections):
    from test_connection import query

    everything, path = [], "/v1/readings"
    while True:
        body = get(connections["api"], path)
        everything.extend(body["data"])
        if "cursor" not in body:
            break
        path = f"/v1/readings?cursor={body['cursor']}"

    in_database = query(connections["ops"], 'SELECT count(*) FROM "readings"')[0][0]
    assert len(everything) == in_database


# -- 14d: refusing is not being down -----------------------------------

def test_a_missing_token_is_a_json_401(connections):
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(connections["api"], "/v1/readings", token=None)
    assert raised.value.code == 401
    # A well-formed body, not a connection failure -- a different thing
    # for a consumer to handle.
    assert json.loads(raised.value.read())["errors"][0]["code"] == "UNAUTHORIZED"


def test_an_unknown_collection_is_404_not_a_crash(connections):
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(connections["api"], "/v1/invoices")
    assert raised.value.code == 404


# -- 14e: money over JSON ----------------------------------------------

def test_money_is_a_string_over_json(connections):
    # JSON has no decimal type, and a float cannot represent 0.10.
    # Sending money as a number would reintroduce, one layer up, the
    # error the column type refuses to allow.
    record = get(connections["api"], "/v1/readings")["data"][0]
    assert isinstance(record["amount"], str)
    assert len(record["amount"].split(".")[1]) == 4


def test_types_json_carries_are_not_stringified(connections):
    record = get(connections["api"], "/v1/readings")["data"][0]
    assert record["note"] is None, "a null became something else"
    assert isinstance(record["settled"], bool)
    assert isinstance(record["quantity"], int)
    assert record["taken_at"].endswith("+00:00"), "the offset was lost"
