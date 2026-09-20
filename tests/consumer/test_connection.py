"""Roadmap 12: can something that is not the simulator connect at all?

Nothing here imports `simulator`. The only input is connections.json.
"""

import socket

import pytest


def connect(details, *, writer=False):
    """Open a connection from the published details alone.

    Deliberately written the way a consumer would write it -- pulling
    keys out of a dict by name -- so a missing or misnamed key fails
    here rather than being quietly worked around.

    THE PASSWORD IS SENT, and this suite had to learn to. When the
    databases started requiring one, a hundred and three tests here
    failed at connect -- which is exactly what a consumer that never
    learned would do on its first real deployment, and the reason the
    requirement is worth having.
    """
    user = details["writer_user"] if writer else details["user"]
    password = details["writer_password"] if writer else details["password"]
    if details["kind"] == "postgresql":
        import psycopg

        return psycopg.connect(host=details["host"], port=details["port"],
                               dbname=details["database"], user=user,
                               password=password)
    import pymysql

    return pymysql.connect(host=details["host"], port=details["port"],
                           database=details["database"], user=user,
                           password=password, charset="utf8mb4")


def query(details, statement):
    connection = connect(details)
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement)
            return cursor.fetchall()
    finally:
        connection.close()


# -- 12a: the published file is enough --------------------------------

def test_the_descriptor_carries_everything_a_driver_needs(database):
    _, details = database
    assert {"kind", "host", "port", "database", "user"} <= set(details)
    assert isinstance(details["port"], int)


def test_a_connection_can_be_opened_from_the_file_alone(database):
    name, details = database
    rows = query(details, "SELECT 1")
    assert rows[0][0] == 1, name


# -- 12c: the advertised database is the business one ------------------

def test_the_advertised_database_holds_the_business_data(database):
    # A real bug once: descriptors advertised the MAINTENANCE database,
    # which exists, accepts connections and contains nothing. A
    # consumer following that connects successfully and finds an empty
    # world, which is worse than failing to connect.
    name, details = database
    assert details["database"] not in ("postgres", "mysql"), name
    rows = query(details, "SELECT count(*) FROM readings")
    assert rows[0][0] > 0, f"{name} advertised a database with no readings in it"


def test_both_engines_hold_the_same_tables(connections):
    tables = {}
    for name in ("ops", "shop"):
        details = connections[name]
        rows = query(details,
                     "SELECT table_name FROM information_schema.tables "
                     f"WHERE table_schema = '{'public' if name == 'ops' else details['database']}' "
                     "AND table_type = 'BASE TABLE'")
        tables[name] = {str(row[0]) for row in rows if not str(row[0]).startswith("_")}
    assert tables["ops"] == tables["shop"] == {"readings", "sources"}


# -- 12d: nothing is reachable off the machine -------------------------

def test_the_silos_listen_only_on_loopback(connections):
    # A simulated business answering on a LAN interface would be a
    # genuinely bad thing to leave running.
    for name, details in connections.items():
        port = details.get("port") or int(str(details.get("base_url", ":0")).rsplit(":", 1)[-1])
        if not port:
            continue
        assert str(details.get("host", "127.0.0.1")) == "127.0.0.1", name

        outward = socket.socket()
        outward.settimeout(2)
        try:
            with pytest.raises(OSError):
                outward.connect((_own_address(), port))
        finally:
            outward.close()


def _own_address() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1; no packet is sent
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


# -- 12e: a silo that has gone --------------------------------------

def test_a_closed_port_is_refused_rather_than_hanging(connections):
    # What a consumer meets when a silo goes down: a refusal it can
    # report, not a hang it has to time out.
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()

    details = {**connections["ops"], "port": port}
    with pytest.raises(Exception) as raised:
        connect(details)
    assert "refused" in str(raised.value).lower() or "connect" in str(raised.value).lower()
