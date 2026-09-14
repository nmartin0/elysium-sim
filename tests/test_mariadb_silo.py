"""Tests for the MariaDB silo.

Discovery and path tests need no server. Lifecycle tests start a real
instance, are marked `mariadb`, and skip cleanly without one.
"""

import os
from pathlib import Path

import pytest

from simulator.ports import PortRegistry
from simulator.silo import SiloError
from simulator.silos.mariadb import (
    MariaDbBinaries,
    MariaDbSilo,
    MariaDbUnavailable,
    _search_sbin,
)

# -- discovery, no server needed -------------------------------------


def test_discovery_accepts_either_fork(tmp_path, monkeypatch):
    # MySQL and MariaDB ship the same tools under both names, speak the
    # same protocol, and the same client works for both. A machine with
    # only the MySQL names installed is a supported machine.
    fake = tmp_path / "bin"
    fake.mkdir()
    for name in ("mysql_install_db", "mysqld", "mysqladmin"):
        target = fake / name
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake))
    monkeypatch.setattr("simulator.silos.mariadb._SBIN_DIRECTORIES", ())
    binaries = MariaDbBinaries.discover()
    assert binaries.daemon == fake / "mysqld"
    assert binaries.admin == fake / "mysqladmin"


def test_discovery_searches_sbin_because_a_user_path_often_omits_it(tmp_path, monkeypatch):
    # THE case this fallback exists for. On many systems /usr/sbin is
    # only on root's PATH, so `command -v mariadbd` comes back empty
    # for exactly the unprivileged user the server has to run as.
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    daemon = sbin / "mariadbd"
    daemon.write_text("#!/bin/sh\n")
    daemon.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("mariadb-install-db", "mariadb-admin"):
        target = bin_dir / name
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)

    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr("simulator.silos.mariadb._SBIN_DIRECTORIES", (str(sbin),))
    assert MariaDbBinaries.discover().daemon == daemon


def test_search_sbin_ignores_a_non_executable(tmp_path, monkeypatch):
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    (sbin / "mariadbd").write_text("")
    monkeypatch.setattr("simulator.silos.mariadb._SBIN_DIRECTORIES", (str(sbin),))
    assert _search_sbin("mariadbd") is None


def test_discovery_explains_itself_when_nothing_is_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr("simulator.silos.mariadb._SBIN_DIRECTORIES", ())
    with pytest.raises(MariaDbUnavailable) as raised:
        MariaDbBinaries.discover()
    assert "mariadb-server" in str(raised.value)


# -- paths, no server needed -----------------------------------------


def _silo(tmp_path: Path, port: int = 5998) -> MariaDbSilo:
    return MariaDbSilo(
        name="shop", data_dir=tmp_path / "shop", port=port,
        binaries=MariaDbBinaries(install_db=Path("/nonexistent/mariadb-install-db"),
                                 daemon=Path("/nonexistent/mariadbd"),
                                 admin=Path("/nonexistent/mariadb-admin")),
    )


def test_the_socket_lives_inside_the_world(tmp_path):
    assert _silo(tmp_path).socket_path.is_relative_to(tmp_path)


def test_starting_without_a_cluster_says_so(tmp_path):
    with pytest.raises(SiloError, match="create it first"):
        _silo(tmp_path).start()


def test_stopping_something_that_never_ran_is_quiet(tmp_path):
    _silo(tmp_path).stop()


def test_terminating_something_that_never_ran_is_quiet(tmp_path):
    _silo(tmp_path).terminate()


def test_the_connection_descriptor_is_server_shaped(tmp_path):
    descriptor = _silo(tmp_path, port=3399).connection("shop")
    assert descriptor.kind == "mariadb"
    assert descriptor.details["port"] == 3399
    assert descriptor.details["database"] == "shop"
    assert descriptor.summary() == "mariadb://127.0.0.1:3399/shop"


# -- the real thing --------------------------------------------------


@pytest.fixture
def running_silo(tmp_path, mariadb_binaries):
    registry = PortRegistry.allocate(tmp_path, ["shop"])
    silo = MariaDbSilo(name="shop", data_dir=tmp_path / "shop",
                       port=registry.port("shop"), binaries=mariadb_binaries)
    silo.create()
    silo.start()
    try:
        yield silo
    finally:
        silo.stop()


@pytest.mark.mariadb
def test_an_instance_starts_and_accepts_connections(running_silo):
    import pymysql

    assert running_silo.is_reachable()
    connection = pymysql.connect(**running_silo.connection_kwargs())
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.mariadb
def test_it_holds_a_web_store_shaped_schema(running_silo):
    # The shape a small business really has on MariaDB: a WooCommerce
    # store, with its own column types and status vocabulary.
    import pymysql

    connection = pymysql.connect(**running_silo.connection_kwargs())
    try:
        with connection.cursor() as cursor:
            cursor.execute("CREATE DATABASE shop")
            cursor.execute("USE shop")
            cursor.execute(
                "CREATE TABLE wp_wc_order_stats ("
                " order_id BIGINT UNSIGNED PRIMARY KEY,"
                " date_created DATETIME NOT NULL,"
                " total_sales DECIMAL(26,8) NOT NULL,"
                " status VARCHAR(32) NOT NULL"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            cursor.execute(
                "INSERT INTO wp_wc_order_stats VALUES (1,'2026-03-02 10:15:00',42.50,'wc-completed')"
            )
            connection.commit()
            cursor.execute("SELECT total_sales, status FROM wp_wc_order_stats")
            total, status = cursor.fetchone()
            assert float(total) == 42.5
            assert status == "wc-completed"
    finally:
        connection.close()


@pytest.mark.mariadb
def test_it_listens_only_on_loopback(running_silo):
    import pymysql

    connection = pymysql.connect(**running_silo.connection_kwargs())
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT @@bind_address")
            assert cursor.fetchone()[0] == "127.0.0.1"
    finally:
        connection.close()


@pytest.mark.mariadb
def test_stop_then_start_again(running_silo):
    running_silo.stop()
    assert not running_silo.is_reachable()
    running_silo.start()
    assert running_silo.is_reachable()


@pytest.mark.mariadb
def test_starting_an_already_running_silo_is_quiet(running_silo):
    running_silo.start()
    assert running_silo.is_reachable()


@pytest.mark.mariadb
def test_terminate_makes_the_port_stop_answering(running_silo):
    running_silo.terminate()
    assert not running_silo.is_reachable()


@pytest.mark.mariadb
def test_stopping_after_a_terminate_is_quiet(running_silo):
    running_silo.terminate()
    running_silo.stop()
    assert not running_silo.is_reachable()


@pytest.mark.mariadb
def test_creating_over_an_existing_cluster_refuses(running_silo):
    with pytest.raises(SiloError, match="already exists"):
        running_silo.create()


@pytest.mark.mariadb
def test_a_failed_start_reports_the_log_rather_than_hiding_it(tmp_path, mariadb_binaries):
    # A start that fails because the port is taken should say so from
    # the server's own log, not leave someone to go find it.
    import socket

    registry = PortRegistry.allocate(tmp_path, ["shop"])
    squatter = socket.socket()
    squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    squatter.bind(("127.0.0.1", registry.port("shop")))
    squatter.listen(1)
    silo = MariaDbSilo(name="shop", data_dir=tmp_path / "shop",
                       port=registry.port("shop"), binaries=mariadb_binaries)
    try:
        silo.create()
        with pytest.raises(SiloError) as raised:
            silo.start()
        assert "mariadb.log" in str(raised.value) or "exited" in str(raised.value)
    finally:
        silo.terminate()
        squatter.close()


@pytest.mark.mariadb
def test_two_technologies_coexist_in_one_world(tmp_path, mariadb_binaries, postgres_binaries):
    # What a small business actually looks like: a web store on MariaDB
    # and a back-office system on PostgreSQL, side by side, each on its
    # own port, neither knowing about the other.
    import psycopg
    import pymysql

    from simulator.silos.postgres import PostgresSilo

    shop = MariaDbSilo(name="shop", data_dir=tmp_path / "shop", port=0, binaries=mariadb_binaries)
    ops = PostgresSilo(name="ops", data_dir=tmp_path / "ops", port=0, binaries=postgres_binaries)
    registry = PortRegistry.allocate_for(tmp_path, [shop, ops])
    shop.port = registry.port("shop")
    ops.port = registry.port("ops")

    try:
        for silo in (shop, ops):
            silo.create()
            silo.start()
        assert shop.is_reachable() and ops.is_reachable()

        connection = pymysql.connect(**shop.connection_kwargs())
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                assert cursor.fetchone()[0] == 1
        finally:
            connection.close()
        with psycopg.connect(**ops.connection_kwargs()) as connection:
            assert connection.execute("SELECT 1").fetchone()[0] == 1

        # One going down leaves the other untouched.
        shop.terminate()
        assert not shop.is_reachable()
        assert ops.is_reachable()
    finally:
        for silo in (shop, ops):
            silo.stop()


def test_ports_are_allocated_per_silo_not_per_technology(tmp_path):
    # Two MariaDB silos in one world are two businesses' systems, not
    # one shared server with two databases.
    silos = [
        MariaDbSilo(name="shop", data_dir=tmp_path / "shop", port=0, binaries=_binaries()),
        MariaDbSilo(name="warehouse", data_dir=tmp_path / "wh", port=0, binaries=_binaries()),
    ]
    registry = PortRegistry.allocate_for(tmp_path, silos)
    assert set(registry.ports) == {"shop", "warehouse"}
    assert len(set(registry.ports.values())) == 2


def _binaries():
    return MariaDbBinaries(install_db=Path("/nonexistent/a"),
                           daemon=Path("/nonexistent/b"),
                           admin=Path("/nonexistent/c"))


def test_root_is_not_required(tmp_path):
    # Unlike PostgreSQL, MariaDB does not refuse to run as root -- but
    # the simulator has no reason to need it either, and this pins that
    # nothing in the path checks for privilege.
    assert os.access(tmp_path, os.W_OK)
    _silo(tmp_path)
