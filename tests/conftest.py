"""Shared fixtures.

The PostgreSQL availability check decides whether tests marked
`postgres` run or skip. Skipping is right rather than failing: a machine
without a server is an environmental fact, not a regression, and most of
the suite is useful without one.
"""

import os

import pytest

from simulator.silos.mariadb import MariaDbBinaries, MariaDbUnavailable
from simulator.silos.postgres import PostgresBinaries, PostgresUnavailable


def _unavailable_reason() -> str | None:
    try:
        PostgresBinaries.discover()
    except PostgresUnavailable as error:
        return str(error)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # Not a missing installation: PostgreSQL refuses to run as root
        # and so does the simulator. Named separately so a root CI run
        # reports the real reason rather than "not installed".
        return "PostgreSQL refuses to run as root; run the suite as an ordinary user"
    return None


def _mariadb_unavailable_reason() -> str | None:
    try:
        MariaDbBinaries.discover()
    except MariaDbUnavailable as error:
        return str(error)
    return None


@pytest.fixture(scope="session")
def mariadb_binaries():
    reason = _mariadb_unavailable_reason()
    if reason:
        pytest.skip(reason)
    return MariaDbBinaries.discover()


@pytest.fixture(scope="session")
def postgres_binaries():
    reason = _unavailable_reason()
    if reason:
        pytest.skip(reason)
    return PostgresBinaries.discover()
