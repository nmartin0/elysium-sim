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

from pathlib import Path

from simulator.silo import Silo, SiloError
from simulator.silos.filedrop import FileDropSilo
from simulator.silos.mariadb import MariaDbSilo
from simulator.silos.postgres import PostgresSilo
from simulator.silos.rest import RestSilo
from simulator.silos.sqlite import SqliteSilo

#: Every silo kind a pack file may declare, by the name it uses. An
#: explicit mapping rather than import scanning: three entries do not
#: need a plugin system, and a greppable dict is what a reader wants
#: when a pack names a kind that does not exist.
SILO_TYPES: dict[str, type[Silo]] = {
    PostgresSilo.kind: PostgresSilo,
    MariaDbSilo.kind: MariaDbSilo,
    SqliteSilo.kind: SqliteSilo,
    FileDropSilo.kind: FileDropSilo,
    RestSilo.kind: RestSilo,
}


def build_silo(kind: str, name: str, data_dir: "Path", port: int | None = None,
               options: dict | None = None) -> Silo:
    """Construct one silo from a pack declaration.

    Lives in this package rather than in whatever is doing the
    building, because the five constructors genuinely differ -- a port
    for the servers and the API, a filename or folder for the
    file-based ones -- and that difference is a fact about the
    technologies. Putting the dispatch outside would mean something
    else knowing which kinds take a port, which is exactly the
    knowledge requires_port already carries.

    Options are passed straight through as keyword arguments, so a
    pack declaring an option a silo does not take fails HERE, naming
    it, rather than as a TypeError from a constructor.
    """
    if kind not in SILO_TYPES:
        raise SiloError(f"unknown silo kind {kind!r}; available: {sorted(SILO_TYPES)}")
    silo_type = SILO_TYPES[kind]
    arguments: dict = dict(options or {})
    if silo_type.requires_port:
        if port is None:
            raise SiloError(f"silo {name!r} is a {kind!r} silo and needs a port")
        arguments["port"] = port
    elif port is not None:
        raise SiloError(f"silo {name!r} is a {kind!r} silo and cannot take a port")
    try:
        return silo_type(name=name, data_dir=data_dir, **arguments)
    except TypeError as error:
        raise SiloError(
            f"silo {name!r} ({kind}) does not accept these options "
            f"{sorted(options or {})}: {error}"
        ) from error


__all__ = ["SILO_TYPES", "FileDropSilo", "MariaDbSilo", "PostgresSilo", "RestSilo",
           "SqliteSilo", "build_silo"]
