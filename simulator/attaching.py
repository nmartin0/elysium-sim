"""
attaching.py  (talking to a world this process did not build)

Every command except `run` arrives at a world that is already there:
`status`, `drift`, `audit`, `verify` and `clean` are all run from a
second terminal, against clusters somebody else started.

Two things are needed and no more. What the world published about
itself, which is connections.json and nothing else -- reading the pack
would be reaching for knowledge a consumer does not have, and reading
the cluster would need the world object this process does not own. And
a silo object that can TALK to a running silo without creating or
starting one, because this process must not try: a second `pg_ctl
start` against a live cluster is how a world gets two postmasters and
no obvious owner.

That connections.json is the only input is what makes `verify`'s
answer worth anything. A check that reached past it -- to the pack, to
a superuser -- would pass on a database no consumer could read.
"""

import json
from pathlib import Path
from typing import Any

#: Written into the world directory. A consumer reads this instead of
#: being told a port by hand.
CONNECTIONS_FILENAME = "connections.json"


def attach(directory: Path) -> dict[str, Any]:
    """Read the connection descriptors a running world published."""
    path = Path(directory) / CONNECTIONS_FILENAME
    if not path.exists():
        raise FileNotFoundError(
            f"no {CONNECTIONS_FILENAME} in {directory}; is a world running there?"
        )
    return json.loads(path.read_text())


def reach(name: str, details: dict, directory: Path) -> Any:
    """A silo object that can talk to an already-running silo.

    Constructed but never created or started: this process did not
    build the world and must not try to. The data directory is passed
    because the file-based kinds are reached by path, and is unused by
    the ones reached by port.
    """
    from simulator.silos import build_silo

    kind = details["kind"]
    port = details.get("port") or details.get("base_url", "").rsplit(":", 1)[-1]
    return build_silo(kind=kind, name=name, data_dir=Path(directory) / name,
                      port=int(port) if port else None)
