"""
Concrete silos: the systems a simulated business actually runs.

Not all of them are databases, and that is the point. A folder that a
bank or a payroll provider drops CSV into is a silo in exactly the
sense that matters -- real operational data, reached directly, that a
platform has to read -- and for a small business it is a more common
integration than any database connection.

One module per technology. Everything that knows a technology
specifically lives in its own module here, so that adding a kind means
adding a file rather than editing a shared dispatch.

These modules own the LIFECYCLE of a silo -- initialising it,
starting it, reaching it, tearing it down -- and nothing about the
shape of the data inside. Rendering a schema as one engine's SQL lives
in simulator/dialect.py, which was written once a second engine
existed and the two had something real to disagree about. Before that
it would have been a seam cut in the dark.
"""

from simulator.silos.filedrop import FileDropSilo
from simulator.silos.mariadb import MariaDbSilo
from simulator.silos.postgres import PostgresSilo
from simulator.silos.rest import RestSilo
from simulator.silos.sqlite import SqliteSilo

#: Every silo kind a pack file may declare, by the name it uses. An
#: explicit mapping rather than import scanning: three entries do not
#: need a plugin system, and a greppable dict is what a reader wants
#: when a pack names a kind that does not exist.
SILO_TYPES = {
    PostgresSilo.kind: PostgresSilo,
    MariaDbSilo.kind: MariaDbSilo,
    SqliteSilo.kind: SqliteSilo,
    FileDropSilo.kind: FileDropSilo,
    RestSilo.kind: RestSilo,
}

__all__ = ["SILO_TYPES", "FileDropSilo", "MariaDbSilo", "PostgresSilo", "RestSilo", "SqliteSilo"]
