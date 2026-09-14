"""Tests for the folder-of-files silo.

The interesting behaviour here is not "can it write a file" but the
publication discipline: a consumer polling the directory must see a
file either absent or complete, and the dialect must look like what
these files really look like rather than what would be tidy.
"""

import csv
import io
import os
import stat

import pytest

from simulator.ports import PortRegistry
from simulator.silo import SiloError
from simulator.silos.filedrop import (
    PARTIAL_SUFFIX,
    TERMINATED_SUFFIX,
    UTF8_BOM,
    FileDropSilo,
)


@pytest.fixture
def silo(tmp_path):
    made = FileDropSilo(name="bank", data_dir=tmp_path)
    made.create()
    return made


# -- lifecycle -------------------------------------------------------

def test_creation_makes_the_folder(silo):
    assert silo.path.is_dir()
    assert silo.is_reachable()


def test_creating_over_an_existing_folder_refuses(silo):
    with pytest.raises(SiloError, match="already exists"):
        silo.create()


def test_starting_and_stopping_do_nothing(silo):
    silo.start()
    silo.stop()
    assert silo.is_reachable()


def test_it_needs_no_port(tmp_path):
    assert FileDropSilo.requires_port is False
    registry = PortRegistry.allocate_for(tmp_path, [FileDropSilo(name="bank", data_dir=tmp_path)])
    assert registry.ports == {}


def test_a_missing_folder_is_not_reachable(silo):
    silo.path.rmdir()
    assert silo.is_reachable() is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_an_unreadable_folder_is_not_reachable(silo):
    # The realistic way a share becomes unusable is a permission change
    # or a dropped mount, not a deletion -- and in both the path is
    # still there, so existence alone would report it healthy.
    silo.path.chmod(0o000)
    try:
        assert silo.is_reachable() is False
    finally:
        silo.path.chmod(stat.S_IRWXU)


def test_the_descriptor_is_path_shaped_and_names_the_encoding(silo):
    descriptor = silo.connection()
    assert descriptor.kind == "filedrop"
    assert descriptor.details["path"] == str(silo.path)
    # A consumer needs to be told about the BOM, because a file with
    # one read as plain utf-8 puts three stray bytes on the first
    # field name.
    assert descriptor.details["encoding"] == "utf-8-sig"


def test_without_a_bom_the_encoding_changes_too(tmp_path):
    silo = FileDropSilo(name="clean", data_dir=tmp_path, bom=False)
    assert silo.connection().details["encoding"] == "utf-8"


# -- publication discipline ------------------------------------------

def test_a_published_file_is_never_visible_half_written(silo, monkeypatch):
    # THE property this silo exists to get right, and it can only be
    # checked DURING the write. A first version asserted the end state
    # -- file published, no partials left behind -- and passed against
    # a version that wrote straight to the final name, because writing
    # in place also ends with a complete file and no partials. That is
    # a test of the outcome both implementations share.
    #
    # So the interleaving is forced instead: every write is intercepted
    # and asked what a consumer polling the directory would see at that
    # exact moment. The payload must never land on the published name.
    import pathlib as _pathlib

    original = _pathlib.Path.write_text
    observations = []

    def observe(self, *args, **kwargs):
        observations.append((self.name, (silo.path / "statement_2026-03-02.csv").exists()))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(_pathlib.Path, "write_text", observe)
    silo.write_csv("statement_2026-03-02.csv", ["date", "amount"],
                   [["2026-03-01", "12.50"], ["2026-03-02", "-4.00"]])

    assert observations, "nothing was written"
    for written_name, published_existed in observations:
        assert written_name.endswith(PARTIAL_SUFFIX), written_name
        assert published_existed is False

    assert silo.listing() == ["statement_2026-03-02.csv"]
    assert not list(silo.path.glob(f"*{PARTIAL_SUFFIX}"))


def test_a_non_atomic_write_really_does_land_on_the_published_name(silo, monkeypatch):
    # The counterpart, and the reason atomic=False exists: a
    # badly-behaved publisher writes straight to the name a consumer is
    # polling, which is what makes torn reads possible.
    import pathlib as _pathlib

    original = _pathlib.Path.write_text
    written = []

    def observe(self, *args, **kwargs):
        written.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(_pathlib.Path, "write_text", observe)
    silo.place("raw.csv", "date,amount\r\n", atomic=False)
    assert written == ["raw.csv"]


def test_listing_hides_partial_writes_that_are_still_in_flight(silo):
    # A consumer WOULD see these -- they are really on disk -- but they
    # are not published, and the silo reports what it published.
    (silo.path / f"statement.csv{PARTIAL_SUFFIX}").write_text("date,amount\r\n")
    assert silo.listing() == []


def test_a_non_atomic_write_is_available_on_purpose(silo):
    # Plenty of real publishers write in place, and the resulting torn
    # reads are a recurring integration failure. Being able to cause it
    # is a property of the upstream being simulated.
    path = silo.place("raw.csv", "date,amount\r\n", atomic=False)
    assert path.exists()
    assert not list(silo.path.glob(f"*{PARTIAL_SUFFIX}"))


def test_publishing_into_an_unreachable_silo_fails_loudly(silo):
    silo.terminate()
    with pytest.raises(SiloError, match="not reachable"):
        silo.place("statement.csv", "a,b\r\n")


@pytest.mark.parametrize("bad", ["../escape.csv", "sub/dir.csv", "back\\slash.csv"])
def test_a_filename_with_a_separator_is_refused(silo, bad):
    # A publisher drops files IN the folder. Allowing a separator would
    # let a pack write outside its own silo.
    with pytest.raises(SiloError, match="plain file name"):
        silo.place(bad, "x")


# -- dialect ---------------------------------------------------------

def test_csv_is_written_the_way_these_files_really_look(silo):
    path = silo.write_csv("export.csv", ["date", "description", "amount"],
                          [["2026-03-01", "Coffee, large", "-4.20"]])
    raw = path.read_bytes()

    # A BOM, because Excel misreads UTF-8 without one and so real
    # exporters emit one.
    assert raw.startswith(UTF8_BOM.encode("utf-8"))
    # CRLF, because these come off Windows.
    assert b"\r\n" in raw
    # And exactly one pair per line. NOTE: this assertion cannot fail
    # on Linux and is kept for what it documents rather than for what
    # it catches. Measured: with newline translation left on,
    # write_text turns "\n" into os.linesep, which IS "\n" here, so
    # CRLF survives unchanged. The CRLFCRLF hazard the newline=""
    # argument prevents only manifests on Windows, where os.linesep is
    # already CRLF. A negative control confirmed the assertion does not
    # fire when newline="" is removed.
    assert b"\r\r\n" not in raw
    # The comma inside a field is quoted rather than breaking the row.
    assert b'"Coffee, large"' in raw


def test_a_consumer_reading_it_as_utf_8_sig_gets_clean_values(silo):
    path = silo.write_csv("export.csv", ["date", "amount"], [["2026-03-01", "-4.20"]])
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["date", "amount"]
    assert rows[1] == ["2026-03-01", "-4.20"]


def test_a_consumer_ignoring_the_bom_sees_it_on_the_first_field(silo):
    # Not a defect -- the condition a real consumer has to handle, and
    # worth pinning so the silo keeps producing it.
    path = silo.write_csv("export.csv", ["date", "amount"], [["2026-03-01", "-4.20"]])
    with path.open("r", encoding="utf-8", newline="") as handle:
        first = next(csv.reader(handle))
    assert first[0] != "date"
    assert first[0].endswith("date")


def test_the_bom_can_be_turned_off(tmp_path):
    silo = FileDropSilo(name="clean", data_dir=tmp_path, bom=False)
    silo.create()
    path = silo.write_csv("export.csv", ["a"], [["1"]])
    assert not path.read_bytes().startswith(UTF8_BOM.encode("utf-8"))


def test_the_line_terminator_can_be_turned_unix(tmp_path):
    silo = FileDropSilo(name="unix", data_dir=tmp_path, line_terminator="\n")
    silo.create()
    raw = silo.write_csv("export.csv", ["a"], [["1"]]).read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"1\n")


def test_many_rows_round_trip(silo):
    rows = [[f"2026-03-{day:02d}", f"{day * 1.5:.2f}"] for day in range(1, 29)]
    path = silo.write_csv("month.csv", ["date", "amount"], rows)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        read_back = list(csv.reader(handle))
    assert read_back[0] == ["date", "amount"]
    assert len(read_back) == 29
    assert read_back[-1] == ["2026-03-28", "42.00"]


# -- termination -----------------------------------------------------

def test_terminate_moves_the_folder_aside_and_is_recoverable(silo):
    silo.write_csv("statement.csv", ["a"], [["1"]])
    silo.terminate()

    assert silo.is_reachable() is False
    moved = silo.path.with_name(silo.path.name + TERMINATED_SUFFIX)
    assert moved.is_dir()

    moved.replace(silo.path)
    assert silo.is_reachable()
    assert silo.listing() == ["statement.csv"]


def test_terminating_an_absent_folder_is_quiet(silo):
    silo.path.rmdir()
    silo.terminate()


def test_listing_an_unreachable_silo_is_empty_rather_than_an_error(silo):
    silo.terminate()
    assert silo.listing() == []


# -- registration ----------------------------------------------------

def test_it_is_registered_as_a_kind():
    from simulator.silos import SILO_TYPES

    assert SILO_TYPES["filedrop"] is FileDropSilo


def test_a_partially_written_file_would_be_a_short_parse(silo):
    # Why the atomic rename matters, demonstrated rather than asserted
    # about: a truncated CSV does not raise, it parses into fewer rows
    # with a mangled last one, which is the failure mode that survives
    # unnoticed.
    complete = io.StringIO()
    writer = csv.writer(complete, lineterminator="\r\n")
    writer.writerow(["date", "amount"])
    for day in range(1, 11):
        writer.writerow([f"2026-03-{day:02d}", day])
    truncated = complete.getvalue()[: len(complete.getvalue()) // 2]

    parsed = list(csv.reader(io.StringIO(truncated)))
    assert len(parsed) < 11
