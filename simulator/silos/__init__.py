"""
Concrete silos: the systems a simulated business actually runs.

One module per technology. Everything that knows a technology
specifically lives in its own module here, so that adding a kind means
adding a file rather than editing a shared dispatch.

There is deliberately no dialect abstraction shared between the SQL
ones. PostgreSQL and MariaDB differ in ways that matter to a schema --
identifier quoting, type names, how a database is created -- and a
common dialect layer written against two engines, before the schemas
that will use it exist, would be guessing at the seam. Each module
owns its own SQL until there is something real to factor out.
"""

from simulator.silos.mariadb import MariaDbSilo
from simulator.silos.postgres import PostgresSilo
from simulator.silos.sqlite import SqliteSilo

#: Every silo kind a pack file may declare, by the name it uses. An
#: explicit mapping rather than import scanning: three entries do not
#: need a plugin system, and a greppable dict is what a reader wants
#: when a pack names a kind that does not exist.
SILO_TYPES = {
    PostgresSilo.kind: PostgresSilo,
    MariaDbSilo.kind: MariaDbSilo,
    SqliteSilo.kind: SqliteSilo,
}

__all__ = ["SILO_TYPES", "MariaDbSilo", "PostgresSilo", "SqliteSilo"]
