"""
loader.py  (reading a pack file, and refusing a wrong one)

EVERYTHING IS CHECKED HERE, BEFORE ANYTHING RUNS. That is the whole
value of this file. A pack naming a column its table does not declare,
a lifecycle transition to a state that was never defined, a curve with
twenty-three hours, a generator that does not exist -- every one of
those is a typo, and the useful place to say so is when the file is
read, with the file and the path named, rather than three hours into a
backfill from inside a tick.

ERRORS CARRY THEIR LOCATION. `PackError` takes a path like
`schemas.dispatch.tables.customers.columns.balance` because a message
saying "precision is required for DECIMAL" is useless in a file with
eighty columns. This costs a parameter on every helper and is worth it.

VALIDATION LIVES WITH LOADING RATHER THAN IN ITS OWN MODULE. They are
the same operation: this file has no notion of a parsed-but-unchecked
pack, because such a thing has no legitimate use. Splitting them would
create one, and something would eventually consume it.

KEYS ARE CHECKED FOR BEING STRINGS AT ALL, which sounds paranoid and
is not. PyYAML follows YAML 1.1, where several bare words are not
strings: `null`, `on`, `off`, `yes` and `no` parse as None, True,
True, False and False. A pack writing `null: false` under a column --
which reads exactly like DDL and is the obvious thing to write -- gets
a mapping keyed by None, and the resulting complaint names a key the
author cannot find in their file. So the nullability key is spelled
`nullable`, and _require_mapping explains the trap rather than letting
it confuse someone.

REFERENCES ARE CHECKED AGAINST WHERE THEY WILL BE EVALUATED. A seed
step runs with no subject and nothing emitted, so a seed generator
saying `{from: subject.store_id}` is a pack error -- and a detectable
one, because generators report what they depend on. Catching that at
load is the clearest illustration of why generators.references()
exists at all.
"""

from pathlib import Path
from typing import Any

import yaml

from simulator.generators import GeneratorError, build
from simulator.lifecycle import Lifecycle, Transition
from simulator.scheduler import validate_curve
from simulator.schema import Column, ColumnType, Schema, Table
from simulator.silos import SILO_TYPES
from simulator.spec.model import Curve, PackSpec, SeedStep, SiloSpec


#: Silo kinds that can hold a schema. Derived from the dialects that
#: exist rather than listed again: a kind with no dialect cannot have
#: tables created in it, and saying so twice is how the two lists drift.
def _relational_kinds() -> set[str]:
    from simulator.dialect import DIALECTS

    return set(DIALECTS)


#: Suffixes accepted in a duration like `4h` or `30m`. Durations appear
#: as min_dwell on lifecycle transitions and read far better than a
#: count of seconds -- `min_dwell: 7d` against `min_dwell: 604800`.
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

#: The namespaces a generator may reference during seeding. A seed step
#: has no subject, nothing picked and nothing emitted, so only the row
#: being built is available.
_SEED_NAMESPACES = frozenset({"row"})


class PackError(Exception):
    """A pack file was malformed, or referred to something absent."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


def load_pack(path: Path) -> PackSpec:
    """Read and fully validate a pack file."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise PackError(str(path), f"is not valid YAML: {error}") from error
    if not isinstance(raw, dict):
        raise PackError(str(path), "must be a mapping at the top level")
    return load_spec(raw)


def load_spec(raw: dict) -> PackSpec:
    """Validate an already-parsed pack, so tests need no file."""
    name = _string(raw, "pack", "pack")
    description = str(raw.get("description", ""))

    silos = _load_silos(_mapping(raw, "silos", "silos"))
    curves = _load_curves(raw.get("curves") or {})
    schemas = _load_schemas(raw.get("schemas") or {}, silos)
    lifecycles = _load_lifecycles(raw.get("lifecycles") or {})
    seed = _load_seed(raw.get("seed") or [], schemas)

    return PackSpec(name=name, description=description, silos=silos,
                    schemas=schemas, curves=curves, lifecycles=lifecycles, seed=seed)


# -- silos -----------------------------------------------------------

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
        if database is not None and not isinstance(database, str):
            raise PackError(path, "database must be a string")
        options = definition.get("options") or {}
        if not isinstance(options, dict):
            raise PackError(path, "options must be a mapping")
        silos[name] = SiloSpec(name=name, kind=kind, database=database, options=options)
    return silos


# -- curves ----------------------------------------------------------

def _load_curves(raw: dict) -> dict[str, Curve]:
    curves = {}
    for name, weights in raw.items():
        path = f"curves.{name}"
        if not isinstance(weights, list):
            raise PackError(path, "a curve must be a list of 24 hourly weights")
        try:
            curves[name] = validate_curve(name, weights)
        except (ValueError, TypeError) as error:
            raise PackError(path, str(error)) from error
    return curves


# -- schemas ---------------------------------------------------------

def _load_schemas(raw: dict, silos: dict[str, SiloSpec]) -> dict[str, Schema]:
    relational = _relational_kinds()
    schemas = {}
    for silo_name, definition in raw.items():
        path = f"schemas.{silo_name}"
        if silo_name not in silos:
            raise PackError(path, f"there is no silo called {silo_name!r}")
        kind = silos[silo_name].kind
        if kind not in relational:
            # A folder of CSV and a JSON API do not have tables. Letting
            # a pack declare a schema for one would produce a pack that
            # looks complete and creates nothing.
            raise PackError(
                path,
                f"silo {silo_name!r} is a {kind!r} silo and cannot hold tables; "
                f"schemas belong to {sorted(relational)} silos"
            )
        if silos[silo_name].database is None:
            raise PackError(
                path, f"silo {silo_name!r} holds a schema, so it must name a database"
            )
        tables = _require_mapping(definition, path).get("tables")
        if not isinstance(tables, dict) or not tables:
            raise PackError(path, "must declare at least one table under `tables`")
        schemas[silo_name] = Schema(tables=tuple(
            _load_table(table_name, table_def, f"{path}.tables.{table_name}")
            for table_name, table_def in tables.items()
        ))
    return schemas


def _load_table(name: str, definition: Any, path: str) -> Table:
    columns = _require_mapping(definition, path).get("columns")
    if not isinstance(columns, dict) or not columns:
        raise PackError(path, "must declare at least one column under `columns`")
    try:
        return Table(name=name, columns=tuple(
            _load_column(column_name, column_def, f"{path}.columns.{column_name}")
            for column_name, column_def in columns.items()
        ))
    except ValueError as error:
        raise PackError(path, str(error)) from error


def _load_column(name: str, definition: Any, path: str) -> Column:
    definition = _require_mapping(definition, path)
    type_name = _string(definition, "type", path)
    try:
        column_type = ColumnType(type_name)
    except ValueError as error:
        raise PackError(
            path,
            f"unknown column type {type_name!r}; available: "
            f"{sorted(member.value for member in ColumnType)}"
        ) from error

    unknown = sorted(set(definition) - {"type", "nullable", "primary_key", "length",
                                        "precision", "scale"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")

    try:
        return Column(
            name=name,
            type=column_type,
            # `nullable`, not `null`. The latter reads better and
            # matches DDL, and YAML 1.1 turns it into the None key --
            # see the module note.
            nullable=bool(definition.get("nullable", True)),
            primary_key=bool(definition.get("primary_key", False)),
            length=definition.get("length"),
            precision=definition.get("precision"),
            scale=definition.get("scale"),
        )
    except ValueError as error:
        raise PackError(path, str(error)) from error


# -- lifecycles ------------------------------------------------------

def _load_lifecycles(raw: dict) -> dict[str, Lifecycle]:
    lifecycles = {}
    for name, definition in raw.items():
        path = f"lifecycles.{name}"
        definition = _require_mapping(definition, path)
        initial = _string(definition, "initial", path)
        states_raw = definition.get("states")
        if not isinstance(states_raw, dict) or not states_raw:
            raise PackError(path, "must declare at least one state under `states`")

        states: dict[str, list[Transition]] = {}
        for state, exits in states_raw.items():
            state_path = f"{path}.states.{state}"
            if exits is None:
                # A terminal state. `churned:` with nothing under it is
                # how a pack says "nothing leaves here", and is more
                # readable than an empty list.
                states[state] = []
                continue
            if not isinstance(exits, list):
                raise PackError(state_path, "must be a list of transitions, or empty")
            states[state] = [
                _load_transition(exit_def, f"{state_path}[{index}]")
                for index, exit_def in enumerate(exits)
            ]
        try:
            lifecycles[name] = Lifecycle(name=name, initial=initial, states=states)
        except ValueError as error:
            raise PackError(path, str(error)) from error
    return lifecycles


def _load_transition(definition: Any, path: str) -> Transition:
    definition = _require_mapping(definition, path)
    unknown = sorted(set(definition) - {"to", "per_hour", "min_dwell"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")
    to_state = _string(definition, "to", path)
    if "per_hour" not in definition:
        raise PackError(path, "needs a per_hour rate")
    try:
        return Transition(
            to_state=to_state,
            per_hour=float(definition["per_hour"]),
            min_dwell_seconds=_duration(definition.get("min_dwell", 0), path),
        )
    except (ValueError, TypeError) as error:
        raise PackError(path, str(error)) from error


def _duration(value: Any, path: str) -> float:
    """Seconds from `4h`, `30m`, `7d`, or a bare number."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or len(value) < 2:
        raise PackError(path, f"{value!r} is not a duration like '4h' or '30m'")
    # The NUMBER is checked first, deliberately. "soon" ends in "n",
    # and reporting an unknown unit 'n' sends someone looking for a
    # typo in a unit they never wrote. Something that is not a number
    # followed by a unit is not a duration at all, and should say so.
    try:
        amount = float(value[:-1])
    except ValueError as error:
        raise PackError(path, f"{value!r} is not a duration like '4h' or '30m'") from error
    unit = value[-1].lower()
    if unit not in _DURATION_UNITS:
        raise PackError(
            path, f"{value!r} has unknown unit {unit!r}; use one of {sorted(_DURATION_UNITS)}"
        )
    return amount * _DURATION_UNITS[unit]


# -- seed ------------------------------------------------------------

def _load_seed(raw: Any, schemas: dict[str, Schema]) -> tuple[SeedStep, ...]:
    if not isinstance(raw, list):
        raise PackError("seed", "must be a list of steps")
    return tuple(
        _load_seed_step(step, f"seed[{index}]", schemas)
        for index, step in enumerate(raw)
    )


def _load_seed_step(definition: Any, path: str, schemas: dict[str, Schema]) -> SeedStep:
    definition = _require_mapping(definition, path)
    unknown = sorted(set(definition) - {"table", "count", "columns"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")

    qualified = _string(definition, "table", path)
    if qualified.count(".") != 1:
        raise PackError(path, f"table {qualified!r} must be written as silo.table")
    silo_name, table_name = qualified.split(".")
    if silo_name not in schemas:
        raise PackError(path, f"no schema is declared for silo {silo_name!r}")
    try:
        table = schemas[silo_name].table(table_name)
    except KeyError as error:
        raise PackError(path, f"silo {silo_name!r} has no table {table_name!r}") from error

    count = definition.get("count", 1)
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise PackError(path, f"count must be a positive whole number, got {count!r}")

    columns = definition.get("columns")
    if not isinstance(columns, dict) or not columns:
        raise PackError(path, "must declare column generators under `columns`")

    _check_seed_columns(table, columns, path)
    return SeedStep(silo=silo_name, table=table_name, count=count, columns=dict(columns))


def _check_seed_columns(table: Table, columns: dict, path: str) -> None:
    declared = {column.name for column in table.columns}
    unknown = sorted(set(columns) - declared)
    if unknown:
        raise PackError(path, f"table {table.name!r} has no column(s) {unknown}")

    # Every column that cannot be null must be generated. A pack that
    # omits one produces rows the engine rejects, which surfaces as a
    # database error mid-seed rather than as a pack problem.
    required = {column.name for column in table.columns if not column.nullable}
    missing = sorted(required - set(columns))
    if missing:
        raise PackError(path, f"no generator for non-null column(s) {missing}")

    for column_name, declaration in columns.items():
        column_path = f"{path}.columns.{column_name}"
        try:
            generator = build(declaration)
        except GeneratorError as error:
            raise PackError(column_path, str(error)) from error
        _check_seed_references(generator.references(), columns, column_path)


def _check_seed_references(references: set[str], columns: dict, path: str) -> None:
    """A seed step may only refer to the row it is building.

    There is no subject, nothing has been picked, and nothing has been
    emitted, so any other namespace is a mistake -- and a detectable
    one, because generators report what they depend on. This is the
    clearest illustration of why references() exists.
    """
    for reference in sorted(references):
        root = reference.split(".")[0]
        if root in _SEED_NAMESPACES:
            continue
        if "." in reference:
            raise PackError(
                path,
                f"cannot refer to {reference!r} while seeding: a seed step has no "
                f"subject and nothing picked or emitted, so only fields of the row "
                f"being built are available"
            )
        if reference not in columns:
            raise PackError(
                path, f"refers to {reference!r}, which this table does not declare"
            )


# -- small helpers ---------------------------------------------------

def _require_mapping(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        raise PackError(path, f"must be a mapping, got {type(value).__name__}")
    _check_keys_are_strings(value, path)
    return value


def _check_keys_are_strings(mapping: dict, path: str) -> None:
    """Catch YAML's implicit typing before it confuses someone.

    A key of None or True did not come from an author writing None or
    True. It came from them writing `null:`, `on:`, `off:`, `yes:` or
    `no:`, which YAML 1.1 -- and therefore PyYAML -- reads as those
    values rather than as strings. The complaint otherwise names a key
    that does not appear anywhere in their file.
    """
    offenders = [key for key in mapping if not isinstance(key, str)]
    if offenders:
        raise PackError(
            path,
            f"has non-string key(s) {offenders}. YAML reads the bare words null, "
            f"on, off, yes and no as values rather than strings, so `null: false` "
            f"becomes a None key. Quote the word, or use the intended spelling "
            f"(nullability is `nullable`)."
        )


def _mapping(raw: dict, key: str, path: str) -> dict:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise PackError(path, f"must be a mapping, got {type(value).__name__}")
    return value


def _string(raw: dict, key: str, path: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise PackError(f"{path}.{key}" if key not in path else path,
                        f"must be a non-empty string, got {value!r}")
    return value


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): validation lives here rather than in its own
# module. Loading and validating are the same operation -- there is no notion
# of a parsed-but-unchecked pack in this codebase, because such a thing has no
# legitimate use, and splitting them would create one that something eventually
# consumes.
#
# RESOLVED: _relational_kinds() derives from DIALECTS rather than listing kinds
# again. A silo with no dialect cannot have tables created in it, and writing
# that fact in two places is how the two lists drift.
#
# RESOLVED: seed generators are checked against the namespaces available DURING
# SEEDING, which is only `row`. A seed step has no subject and nothing emitted,
# so `{from: subject.store_id}` is a pack error catchable at load. This is the
# clearest use of generators.references() and the reason it exists.
#
# DEFERRED (known, intentional, not yet built): a non-null column with a
# database DEFAULT still has to be generated, because the schema layer has no
# notion of defaults. That is a schema-layer gap rather than a loader one.
#
# DEFERRED: no check that a seed step's declared count is reachable -- a pack
# asking for 500 rows into a table with a primary key generated by `choice`
# over three options will fail at insert, not at load. Detecting it means
# reasoning about generator cardinality, which is a real analysis and not worth
# it until a pack hits the problem.
#
# DEFERRED: _string()'s path handling is slightly awkward (it appends the key
# unless the key already appears in the path) because some callers pass a path
# that already names the field. Worth tidying when a third pattern appears.
