"""Watches declared in a pack file.

The oracle keeps an independent record of what was true and when, and
it is the only thing that can catch a RESCALE: a migration that
multiplies a column by a hundred raises nothing, breaks no type, and
leaves every read succeeding.

Until now that record could only be asked for in Python, which means
the person running a training world could not ask for it at all.
"""

import pytest

from simulator.spec import load_spec
from simulator.spec.values import PackError

BASE = {
    "pack": "watched",
    "silos": {"ops": {"kind": "postgresql", "database": "ops"}},
    "schemas": {"ops": {"tables": {"bills": {"columns": {
        "bill_id": {"type": "text", "length": 64, "primary_key": True,
                    "nullable": False},
        "total": {"type": "decimal", "precision": 19, "scale": 4, "nullable": False},
        "note": {"type": "text", "length": 64},
    }}}}},
}


def watching(*entries):
    return load_spec({**BASE, "watches": list(entries)})


def test_a_pack_can_say_which_numbers_matter():
    pack = watching({"silo": "ops", "table": "bills", "column": "total"})
    assert len(pack.watches) == 1
    watch = pack.watches[0]
    assert watch.name == "sum(ops.bills.total)"
    assert watch.aggregate == "sum", "sum is the sensible default for a number"


def test_a_pack_without_watches_pays_for_none():
    # The oracle costs a query per watch per tick, and a world nobody
    # is checking should not pay for it.
    assert load_spec(BASE).watches == ()


def test_a_watch_may_choose_its_aggregate():
    pack = watching({"silo": "ops", "table": "bills", "column": "total",
                     "aggregate": "max"})
    assert pack.watches[0].name == "max(ops.bills.total)"


def test_counting_works_on_a_column_that_is_not_a_number():
    # `count` counts rows and does not care what is in them, which is
    # what makes it the exception.
    pack = watching({"silo": "ops", "table": "bills", "column": "note",
                     "aggregate": "count"})
    assert pack.watches[0].name == "count(ops.bills.note)"


# -- what it refuses ---------------------------------------------------

def test_a_watch_naming_something_that_is_not_there_is_refused():
    # A watch that sampled nothing would report a number that never
    # existed, and the oracle's whole value is being trustworthy about
    # the past.
    for entry, message in (
        ({"silo": "nope", "table": "bills", "column": "total"}, "no schema"),
        ({"silo": "ops", "table": "nope", "column": "total"}, "no table"),
        ({"silo": "ops", "table": "bills", "column": "nope"}, "no column"),
    ):
        with pytest.raises(PackError, match=message):
            watching(entry)


def test_summing_text_is_refused_rather_than_left_to_the_driver():
    # Summing a text column is a driver error at the first tick, and a
    # min or max over one answers a question about alphabetical order
    # that nobody asked.
    for aggregate in ("sum", "min", "max"):
        with pytest.raises(PackError, match="only count works"):
            watching({"silo": "ops", "table": "bills", "column": "note",
                      "aggregate": aggregate})


def test_an_unknown_aggregate_lists_the_real_ones():
    with pytest.raises(PackError, match="not an aggregate"):
        watching({"silo": "ops", "table": "bills", "column": "total",
                  "aggregate": "average"})


def test_the_same_watch_twice_is_refused():
    # Two identical watches cost two queries a tick and produce one
    # series, so the second is either a mistake or a misunderstanding.
    entry = {"silo": "ops", "table": "bills", "column": "total"}
    with pytest.raises(PackError, match="same watch twice"):
        watching(entry, dict(entry))


def test_a_malformed_watches_block_is_refused():
    for raw, message in (([], "non-empty list"), ("total", "non-empty list")):
        with pytest.raises(PackError, match=message):
            load_spec({**BASE, "watches": raw})
    with pytest.raises(PackError, match="does not understand"):
        watching({"silo": "ops", "table": "bills", "column": "total", "every": "1h"})
