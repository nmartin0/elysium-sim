"""Tests for the file-based silo, and for the contract all silos share.

The SQLite silo is where the Silo abstraction is under most strain --
no port, no process, nothing to start -- so it is also where the
contract is most worth pinning. If the abstraction only fits servers,
it fails here first.
"""

import sqlite3

import pytest

from simulator.ports import PortRegistry
from simulator.silo import ConnectionDescriptor, Silo, SiloError
from simulator.silos import SILO_TYPES, FileDropSilo, MariaDbSilo, PostgresSilo, SqliteSilo
from simulator.silos.sqlite import TERMINATED_SUFFIX


@pytest.fixture
def silo(tmp_path):
    made = SqliteSilo(name="pos", data_dir=tmp_path)
    made.create()
    return made


# -- the file-based lifecycle ----------------------------------------

def test_creation_makes_a_real_database(silo):
    assert silo.path.exists()
    with silo.connect() as connection:
        connection.execute("CREATE TABLE t (a TEXT)")
        connection.execute("INSERT INTO t VALUES ('x')")
        connection.commit()
        assert connection.execute("SELECT a FROM t").fetchone()["a"] == "x"


def test_it_is_created_in_wal_mode(silo):
    # A persistent property of the file, so a consumer picks it up with
    # no configuration. Without it, a reader intermittently blocks on
    # the simulator's own writes -- contention that is an artefact of
    # the tool rather than of the business being simulated.
    with silo.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_starting_and_stopping_do_nothing_and_that_is_correct(silo):
    # Not stubs. A file is openable or it is not, and the contract
    # allows this so the implementation can be honest instead of
    # faking a daemon.
    silo.start()
    assert silo.is_reachable()
    silo.stop()
    assert silo.is_reachable()


def test_creating_over_an_existing_file_refuses(silo):
    with pytest.raises(SiloError, match="already exists"):
        silo.create()


def test_it_needs_no_port(tmp_path):
    assert SqliteSilo.requires_port is False
    assert FileDropSilo.requires_port is False
    assert SqliteSilo(name="pos", data_dir=tmp_path).connection().details.keys() == {"path"}


# -- reachability ----------------------------------------------------

def test_a_missing_file_is_not_reachable(silo):
    silo.path.unlink()
    assert silo.is_reachable() is False


def test_a_zero_byte_file_left_by_a_careless_reader_is_not_reachable(silo):
    # THE case existence checking gets wrong. A bare sqlite3.connect()
    # on a missing path CREATES the file, so anything that probed a
    # terminated silo leaves a zero-byte database behind and an
    # exists() check goes green permanently afterwards.
    silo.path.unlink()
    sqlite3.connect(silo.path).close()
    assert silo.path.exists()
    assert silo.is_reachable() is False


def test_a_file_that_is_not_a_database_is_not_reachable(silo):
    silo.path.write_bytes(b"this is not a database")
    assert silo.is_reachable() is False


def test_connect_refuses_to_recreate_a_missing_file(silo):
    silo.path.unlink()
    with pytest.raises(SiloError, match="cannot open"):
        silo.connect()
    assert not silo.path.exists()


# -- termination -----------------------------------------------------

def test_terminate_moves_the_file_aside_rather_than_deleting(silo):
    # The realistic failure for a desktop application's database is a
    # backup script or a sync client moving the file, not a deletion --
    # and a recoverable condition is more useful to test against.
    with silo.connect() as connection:
        connection.execute("CREATE TABLE t (a TEXT)")
        connection.commit()

    silo.terminate()

    assert silo.is_reachable() is False
    moved = silo.path.with_suffix(silo.path.suffix + TERMINATED_SUFFIX)
    assert moved.exists()

    # And it really is recoverable, data intact.
    moved.replace(silo.path)
    assert silo.is_reachable()
    with silo.connect() as connection:
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE name='t'").fetchone()[0] == 1


def test_terminating_an_absent_silo_is_quiet(silo):
    silo.path.unlink()
    silo.terminate()


# -- the shared contract ---------------------------------------------

def test_every_registered_kind_implements_the_contract():
    # The check that keeps the abstraction real: a kind that satisfies
    # the registry but not the contract would fail only when a pack
    # happened to use it.
    for name, silo_type in SILO_TYPES.items():
        assert issubclass(silo_type, Silo), name
        assert silo_type.kind == name
        assert isinstance(silo_type.requires_port, bool), name
        for method in ("create", "start", "stop", "is_reachable", "connection", "terminate"):
            assert callable(getattr(silo_type, method)), f"{name}.{method}"


def test_the_registry_covers_the_kinds_that_exist():
    assert set(SILO_TYPES) == {"postgresql", "mariadb", "sqlite", "filedrop"}
    assert SILO_TYPES["sqlite"] is SqliteSilo
    assert SILO_TYPES["postgresql"] is PostgresSilo
    assert SILO_TYPES["mariadb"] is MariaDbSilo
    assert SILO_TYPES["filedrop"] is FileDropSilo


def test_server_kinds_need_ports_and_file_kinds_do_not():
    # The distinction the port registry is driven by.
    assert PostgresSilo.requires_port is True
    assert MariaDbSilo.requires_port is True
    assert SqliteSilo.requires_port is False
    assert FileDropSilo.requires_port is False


def test_a_descriptor_summarises_itself_by_shape():
    server = ConnectionDescriptor("postgresql", {"host": "127.0.0.1", "port": 5432, "database": "pos"})
    assert server.summary() == "postgresql://127.0.0.1:5432/pos"
    assert ConnectionDescriptor("sqlite", {"path": "/tmp/pos.db"}).summary() == "sqlite:/tmp/pos.db"


# -- ports are allocated only for silos that need them ----------------

def test_allocation_skips_file_based_silos(tmp_path):
    silos = [
        PostgresSilo(name="ops", data_dir=tmp_path / "ops", port=0,
                     binaries=_fake_postgres_binaries()),
        SqliteSilo(name="pos", data_dir=tmp_path / "pos"),
    ]
    registry = PortRegistry.allocate_for(tmp_path, silos)
    assert set(registry.ports) == {"ops"}


def test_a_world_of_only_file_silos_allocates_nothing(tmp_path):
    # Correct rather than an empty case to work around: there is no
    # port to pin because nothing listens.
    registry = PortRegistry.allocate_for(tmp_path, [SqliteSilo(name="pos", data_dir=tmp_path)])
    assert registry.ports == {}


def _fake_postgres_binaries():
    from pathlib import Path

    from simulator.silos.postgres import PostgresBinaries

    return PostgresBinaries(initdb=Path("/nonexistent/initdb"), pg_ctl=Path("/nonexistent/pg_ctl"))
