"""
silos.py  (the systems a business runs, before anything is in them)

The shortest section and the first one read, because everything after
it is anchored to a silo name: a schema belongs to one, an emission
writes to one, a migration changes one. Getting a name wrong here
would otherwise surface much later as "no schema is declared for silo
'dispatc'", which names the wrong problem.

WHAT IT REFUSES is a silo declared with a database it cannot hold. A
folder of CSV files has no databases in it, and a pack saying
otherwise is describing a system that does not exist -- so `database`
on a filedrop is an error rather than something quietly ignored. The
list of kinds that CAN hold one is derived from the dialects rather
than written again, because a kind with no dialect cannot have tables
created in it and writing that fact twice is how two lists drift.
"""


from simulator.silos import SILO_TYPES
from simulator.spec.model import SiloSpec
from simulator.spec.schemas import _relational_kinds
from simulator.spec.values import PackError, _require_mapping, _string


def _load_withheld(raw: object, path: str, kind: str,
                   database: str | None) -> tuple[str, ...]:
    """Tables the consumer accounts may not read.

    A real deployment rarely grants a reporting tool everything --
    payroll and audit tables are the usual exceptions -- and a consumer
    meeting a table it can FIND but cannot select from is a genuine
    production failure. Reproducing it needs a pack to be able to say
    so.

    Whether the named tables exist is checked later, once the schemas
    are loaded: silos are read first, so nothing here knows what tables
    there will be. See _check_withheld_tables.
    """
    if raw is None:
        return ()
    if kind not in _relational_kinds() or database is None:
        raise PackError(
            path, f"a {kind!r} silo has no tables to withhold")
    if not isinstance(raw, list) or not raw:
        raise PackError(f"{path}.withheld", "must be a non-empty list of table names")
    names = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise PackError(f"{path}.withheld", f"{entry!r} is not a table name")
        names.append(entry)
    if len(set(names)) != len(names):
        raise PackError(f"{path}.withheld", "names a table twice")
    return tuple(names)


def _load_silos(raw: dict) -> dict[str, SiloSpec]:
    if not raw:
        raise PackError("silos", "a pack must declare at least one silo")
    silos = {}
    for name, definition in raw.items():
        path = f"silos.{name}"
        definition = _require_mapping(definition, path)
        kind = _string(definition, "kind", path)
        if kind not in SILO_TYPES:
            raise PackError(path, f"unknown silo kind {kind!r}; available: {sorted(SILO_TYPES)}")
        database = definition.get("database")
        if database is not None:
            if not isinstance(database, str):
                raise PackError(path, "database must be a string")
            if kind not in _relational_kinds():
                raise PackError(
                    path,
                    f"a {kind!r} silo holds no databases; remove `database`"
                )
        options = definition.get("options") or {}
        if not isinstance(options, dict):
            raise PackError(path, "options must be a mapping")
        withheld = _load_withheld(definition.get("withheld"), path, kind, database)
        silos[name] = SiloSpec(name=name, kind=kind, database=database,
                               options=options, withheld=withheld)
    return silos
