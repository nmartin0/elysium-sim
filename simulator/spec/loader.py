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

from simulator.event import (
    AdjustEffect,
    Event,
    ExposeEmission,
    InsertEmission,
    PeriodicTrigger,
    PublishEmission,
    RateTrigger,
    TransitionTrigger,
    UpdateEmission,
)
from simulator.generators import GeneratorError, build
from simulator.lifecycle import Lifecycle, Transition
from simulator.scheduler import FLAT, validate_curve
from simulator.schema import Column, ColumnType, Schema, Table
from simulator.silos import SILO_TYPES
from simulator.spec.model import (
    Curve,
    LifecyclePersistence,
    PackSpec,
    SeedStep,
    SiloSpec,
)

#: Column types an effect may adjust. Adjusting text or a date is not
#: a thing a business does, and silently producing SQL the engine
#: rejects would be worse than refusing the pack.
_NUMERIC_TYPES = frozenset({ColumnType.INTEGER, ColumnType.BIGINT, ColumnType.DECIMAL})

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
    persistence = _load_persistence(raw.get("lifecycles") or {}, schemas)
    seed = _load_seed(raw.get("seed") or [], schemas)
    events = _load_events(raw.get("events") or {}, schemas, curves, lifecycles,
                          persistence, silos)

    return PackSpec(name=name, description=description, silos=silos,
                    schemas=schemas, curves=curves, lifecycles=lifecycles,
                    persistence=persistence, seed=seed, events=events)


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
        unknown = sorted(set(definition) - {"initial", "states", "persisted_to",
                                            "state_column"})
        if unknown:
            raise PackError(path, f"does not understand {unknown}")
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


def _load_persistence(raw: dict, schemas: dict[str, Schema]) -> dict[str, LifecyclePersistence]:
    persistence = {}
    for name, definition in raw.items():
        path = f"lifecycles.{name}"
        if "persisted_to" not in definition:
            if "state_column" in definition:
                raise PackError(
                    path, "declares a state_column but no persisted_to table to write it to"
                )
            continue
        qualified = _string(definition, "persisted_to", path)
        if qualified.count(".") != 1:
            raise PackError(path, f"persisted_to {qualified!r} must be written as silo.table")
        silo_name, table_name = qualified.split(".")
        if silo_name not in schemas:
            raise PackError(path, f"no schema is declared for silo {silo_name!r}")
        try:
            table = schemas[silo_name].table(table_name)
        except KeyError as error:
            raise PackError(path, f"silo {silo_name!r} has no table {table_name!r}") from error

        key = table.primary_key()
        if key is None:
            # Without one there is no way to find the row again when
            # the entity moves.
            raise PackError(
                path,
                f"table {table_name!r} has no primary key, so a lifecycle cannot be "
                f"persisted to it"
            )
        state_column = _string(definition, "state_column", path)
        column = next((c for c in table.columns if c.name == state_column), None)
        if column is None:
            raise PackError(path, f"table {table_name!r} has no column {state_column!r}")
        if column.type is not ColumnType.TEXT:
            raise PackError(
                path,
                f"{state_column!r} is {column.type.value}; a state column holds names "
                f"and must be text"
            )
        persistence[name] = LifecyclePersistence(
            silo=silo_name, table=table_name,
            id_column=key.name, state_column=state_column,
        )
    return persistence


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
    unknown = sorted(set(definition) - {"table", "count", "columns", "per"})
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

    per = definition.get("per")
    subject_columns: set[str] = set()
    if per is not None:
        if "count" in definition:
            raise PackError(
                path, "a step with `per` writes one row per subject, so it takes no count"
            )
        if not isinstance(per, str):
            raise PackError(path, "per must be written as silo.table")
        subject_columns = _subject_columns(per, f"{path}.per", schemas)

    count = definition.get("count", 1)
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise PackError(path, f"count must be a positive whole number, got {count!r}")

    columns = definition.get("columns")
    if not isinstance(columns, dict) or not columns:
        raise PackError(path, "must declare column generators under `columns`")

    _check_seed_columns(table, columns, path, subject_columns, per is not None)
    return SeedStep(silo=silo_name, table=table_name, count=count, per=per,
                    columns=dict(columns))


def _check_seed_columns(table: Table, columns: dict, path: str,
                        subject_columns: set[str], has_subject: bool) -> None:
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
        _check_seed_references(generator.references(), columns, column_path,
                               subject_columns, has_subject)


def _check_seed_references(references: set[str], columns: dict, path: str,
                           subject_columns: set[str], has_subject: bool) -> None:
    """A seed step may refer to the row it is building, and its subject.

    Nothing has been picked and nothing has been emitted, so those
    namespaces are a mistake -- and a detectable one, because
    generators report what they depend on. This is the clearest
    illustration of why references() exists.
    """
    for reference in sorted(references):
        root = reference.split(".")[0]
        if root in _SEED_NAMESPACES:
            continue
        if root == "subject":
            if not has_subject:
                raise PackError(
                    path, f"refers to {reference!r}, but this step has no `per`"
                )
            parts = reference.split(".")
            if len(parts) != 2 or parts[1] not in subject_columns:
                raise PackError(
                    path,
                    f"refers to {reference!r}, but the subject table has columns "
                    f"{sorted(subject_columns)}"
                )
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


# -- events ----------------------------------------------------------

def _load_events(raw: Any, schemas: dict[str, Schema], curves: dict[str, Curve],
                 lifecycles: dict, persistence: dict,
                 silos: dict[str, SiloSpec]) -> tuple[Event, ...]:
    if not isinstance(raw, dict):
        raise PackError("events", "must be a mapping of event name to declaration")
    return tuple(
        _load_event(name, definition, f"events.{name}", schemas, curves,
                    lifecycles, persistence, silos)
        for name, definition in raw.items()
    )


def _load_event(name: str, definition: Any, path: str, schemas: dict[str, Schema],
                curves: dict[str, Curve], lifecycles: dict, persistence: dict,
                silos: dict[str, SiloSpec]) -> Event:
    definition = _require_mapping(definition, path)
    unknown = sorted(set(definition) - {"rate_per_hour", "per", "curve", "emits",
                                        "effects", "lifecycle", "entering", "every"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")

    by_transition = "lifecycle" in definition or "entering" in definition
    by_period = "every" in definition
    declared = [name for name, present in
                (("rate_per_hour", "rate_per_hour" in definition),
                 ("lifecycle/entering", by_transition),
                 ("every", by_period)) if present]
    if len(declared) > 1:
        raise PackError(
            path,
            f"declares {declared}, but an event fires one way: on a rate, on a "
            f"transition, or on a period"
        )
    if not declared:
        raise PackError(path, "needs a rate_per_hour, a lifecycle and entering, or every")

    if by_period:
        every = _duration(definition["every"], f"{path}.every")
        if every <= 0:
            raise PackError(f"{path}.every", "must be a positive interval")
        return _finish_event(name, PeriodicTrigger(every_seconds=every), definition,
                             path, schemas, set(), False, lifecycles, persistence,
                             silos)

    subject_columns: set[str] = set()
    per = definition.get("per")

    if by_transition:
        trigger, subject_columns = _transition_trigger(definition, path, lifecycles,
                                                       persistence)
        if per is not None:
            raise PackError(
                path, "a transition event happens to the entity that moved, not to a `per`"
            )
        return _finish_event(name, trigger, definition, path, schemas, subject_columns,
                             True, lifecycles, persistence, silos)

    rate = definition["rate_per_hour"]
    if not isinstance(rate, int | float) or isinstance(rate, bool) or rate <= 0:
        raise PackError(path, f"rate_per_hour must be a positive number, got {rate!r}")

    if per is not None:
        if not isinstance(per, str):
            raise PackError(path, "per must be written as silo.table")
        subject_columns = _subject_columns(per, f"{path}.per", schemas)

    curve_name = definition.get("curve")
    if curve_name is not None and curve_name not in curves:
        raise PackError(
            f"{path}.curve",
            f"no curve called {curve_name!r}; this pack declares {sorted(curves)}"
        )

    return _finish_event(
        name,
        RateTrigger(rate_per_hour=float(rate), per=per,
                    curve=curves[curve_name] if curve_name else FLAT,
                    stream=f"event.{name}"),
        definition, path, schemas, subject_columns, per is not None,
        lifecycles, persistence, silos,
    )


def _transition_trigger(definition: dict, path: str, lifecycles: dict,
                        persistence: dict) -> tuple[TransitionTrigger, set[str]]:
    lifecycle_name = _string(definition, "lifecycle", path)
    if lifecycle_name not in lifecycles:
        raise PackError(
            path, f"no lifecycle called {lifecycle_name!r}; "
                  f"this pack declares {sorted(lifecycles)}"
        )
    entering = _string(definition, "entering", path)
    states = lifecycles[lifecycle_name].states
    if entering not in states:
        raise PackError(
            f"{path}.entering",
            f"lifecycle {lifecycle_name!r} has no state {entering!r}; "
            f"it has {sorted(states)}"
        )
    if not any(transition.to_state == entering
               for exits in states.values() for transition in exits):
        # A state nothing can reach would make an event that can never
        # fire -- silently, since a pack that does nothing looks the
        # same as one whose rates are simply low.
        raise PackError(
            f"{path}.entering",
            f"nothing transitions INTO {entering!r}, so this event could never fire"
        )

    where = persistence.get(lifecycle_name)
    subject_columns = {"state", "previous_state",
                       where.id_column if where else "entity_id"}
    return TransitionTrigger(lifecycle=lifecycle_name, entering=entering), subject_columns


def _finish_event(name: str, trigger, definition: dict, path: str,
                  schemas: dict[str, Schema], subject_columns: set[str],
                  has_subject: bool, lifecycles: dict, persistence: dict,
                  silos: dict[str, SiloSpec]) -> Event:
    """The half that is the same however an event is triggered."""
    emits = definition.get("emits")
    if not isinstance(emits, list) or not emits:
        raise PackError(path, "must declare at least one emission under `emits`")

    emissions = []
    emitted_so_far: set[str] = set()
    for index, emit_def in enumerate(emits):
        emission = _load_emission(emit_def, f"{path}.emits[{index}]", schemas,
                                  subject_columns, has_subject, emitted_so_far,
                                  lifecycles, persistence, silos)
        emissions.append(emission)
        emitted_so_far.add(emission.qualified)

    effects = tuple(
        _load_effect(effect_def, f"{path}.effects[{index}]", schemas,
                     subject_columns, has_subject, emitted_so_far)
        for index, effect_def in enumerate(definition.get("effects") or [])
    )
    return Event(name=name, trigger=trigger, emissions=tuple(emissions), effects=effects)


def _subject_columns(qualified: str, path: str, schemas: dict[str, Schema]) -> set[str]:
    if qualified.count(".") != 1:
        raise PackError(path, f"{qualified!r} must be written as silo.table")
    silo_name, table_name = qualified.split(".")
    if silo_name not in schemas:
        raise PackError(path, f"no schema is declared for silo {silo_name!r}")
    try:
        table = schemas[silo_name].table(table_name)
    except KeyError as error:
        raise PackError(path, f"silo {silo_name!r} has no table {table_name!r}") from error
    return {column.name for column in table.columns}


def _load_emission(definition: Any, path: str, schemas: dict[str, Schema],
                   subject_columns: set[str], has_subject: bool,
                   emitted_so_far: set[str], lifecycles: dict, persistence: dict,
                   silos: dict[str, SiloSpec],
                   ) -> InsertEmission | UpdateEmission | PublishEmission | ExposeEmission:
    definition = _require_mapping(definition, path)
    if "publish" in definition:
        return _load_publish(definition, path, schemas, silos, subject_columns,
                             has_subject, emitted_so_far)
    if "expose" in definition:
        return _load_expose(definition, path, schemas, silos)
    if "update" in definition:
        return _load_update(definition, path, schemas, subject_columns, has_subject,
                            emitted_so_far)
    unknown = sorted(set(definition) - {"table", "columns", "repeat", "spawns"})
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

    low, high = _repeat(definition.get("repeat", 1), path)

    columns = definition.get("columns")
    if not isinstance(columns, dict) or not columns:
        raise PackError(path, "must declare column generators under `columns`")
    _check_emission_columns(table, columns, path, subject_columns, has_subject,
                            emitted_so_far)

    spawns = definition.get("spawns")
    key_column = None
    if spawns is not None:
        if spawns not in lifecycles:
            raise PackError(
                path,
                f"no lifecycle called {spawns!r}; this pack declares {sorted(lifecycles)}"
            )
        where = persistence.get(spawns)
        if where is not None and where.qualified != qualified:
            # The entity's id is this row's primary key, so when the
            # lifecycle says where it lives, the row has to be there.
            raise PackError(
                path,
                f"lifecycle {spawns!r} is persisted to {where.qualified!r}, so it "
                f"cannot be started by an emission into {qualified!r}"
            )
        # A lifecycle with no persisted_to is still spawnable: it drives
        # behaviour without the business system having a column for it,
        # which is a legitimate thing for a pack to want. It was briefly
        # refused here, which made such a lifecycle declarable and
        # impossible to instantiate -- found by a test whose premise
        # turned out to be unreachable.
        key = table.primary_key()
        if key is None:
            raise PackError(
                path,
                f"spawning {spawns!r} needs {qualified!r} to have a primary key, which "
                f"is the id the entity is tracked by"
            )
        key_column = key.name
        # No check that the key column has a generator: it is the
        # primary key, so it cannot be nullable, so the non-null check
        # above has already required one. A second check here would be
        # unreachable.

    return InsertEmission(
        silo=silo_name, table=table_name,
        columns={name: build(spec) for name, spec in columns.items()},
        repeat_min=low, repeat_max=high, spawns=spawns, key_column=key_column,
    )


def _load_expose(definition: dict, path: str, schemas: dict[str, Schema],
                 silos: dict[str, SiloSpec]) -> ExposeEmission:
    """An emission that publishes a collection through a REST silo."""
    unknown = sorted(set(definition) - {"expose", "collection", "rows_from", "columns"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")

    target = _string(definition, "expose", path)
    if target not in silos:
        raise PackError(path, f"there is no silo called {target!r}")
    if silos[target].kind != "rest":
        raise PackError(
            path,
            f"silo {target!r} is a {silos[target].kind!r} silo; exposing a collection "
            f"needs a rest silo"
        )

    collection = _string(definition, "collection", path)
    if not collection.replace("_", "").isalnum():
        # It becomes a URL path segment, so it has to survive being one.
        raise PackError(
            f"{path}.collection",
            f"{collection!r} becomes a URL path segment and must be alphanumeric "
            f"or underscored"
        )

    source_silo, source_table, columns = _source_rows(definition, path, schemas)
    return ExposeEmission(silo=target, collection=collection, source_silo=source_silo,
                          source_table=source_table, columns=columns)


def _source_rows(definition: dict, path: str,
                 schemas: dict[str, Schema]) -> tuple[str, str, tuple[str, ...]]:
    """Where an export's rows come from, and which columns it takes.

    Shared by publishing a file and exposing a collection, because the
    question is the same one and answering it twice is how the two
    would eventually disagree about what `rows_from` means.
    """
    source = _string(definition, "rows_from", path)
    if source.count(".") != 1:
        raise PackError(path, f"rows_from {source!r} must be written as silo.table")
    silo_name, table_name = source.split(".")
    if silo_name not in schemas:
        raise PackError(path, f"no schema is declared for silo {silo_name!r}")
    try:
        table = schemas[silo_name].table(table_name)
    except KeyError as error:
        raise PackError(path, f"silo {silo_name!r} has no table {table_name!r}") from error

    columns = definition.get("columns")
    if not isinstance(columns, list) or not columns:
        raise PackError(
            path,
            "must list the columns to export. Declared rather than `all`, because an "
            "export is a contract with whoever reads it and should not silently gain "
            "a column when the table does."
        )
    missing = sorted(set(columns) - {column.name for column in table.columns})
    if missing:
        raise PackError(path, f"table {table_name!r} has no column(s) {missing}")
    return silo_name, table_name, tuple(columns)


def _load_publish(definition: dict, path: str, schemas: dict[str, Schema],
                  silos: dict[str, SiloSpec], subject_columns: set[str],
                  has_subject: bool, emitted_so_far: set[str]) -> PublishEmission:
    """An emission that writes a file into a file-drop silo."""
    unknown = sorted(set(definition) - {"publish", "filename", "rows_from", "columns"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")

    target = _string(definition, "publish", path)
    if target not in silos:
        raise PackError(path, f"there is no silo called {target!r}")
    if silos[target].kind != "filedrop":
        raise PackError(
            path,
            f"silo {target!r} is a {silos[target].kind!r} silo; publishing a file "
            f"needs a filedrop silo"
        )

    source_silo, source_table, columns = _source_rows(definition, path, schemas)

    if "filename" not in definition:
        raise PackError(path, "needs a `filename`")
    try:
        filename = build(definition["filename"])
    except GeneratorError as error:
        raise PackError(f"{path}.filename", str(error)) from error
    # Validated against the facts a publication offers, not against a
    # table's columns: there is no row being built here except the one
    # the emission makes for this purpose.
    facts = dict.fromkeys(PublishEmission.FACTS)
    for reference in sorted(filename.references()):
        _check_event_reference(reference, facts, f"{path}.filename", subject_columns,
                               has_subject, emitted_so_far)

    return PublishEmission(silo=target, filename=filename, source_silo=source_silo,
                           source_table=source_table, columns=columns)


def _load_update(definition: dict, path: str, schemas: dict[str, Schema],
                 subject_columns: set[str], has_subject: bool,
                 emitted_so_far: set[str]) -> UpdateEmission:
    """An emission that revises a row rather than writing one."""
    unknown = sorted(set(definition) - {"update", "columns", "where"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")

    qualified = _string(definition, "update", path)
    if qualified.count(".") != 1:
        raise PackError(path, f"table {qualified!r} must be written as silo.table")
    silo_name, table_name = qualified.split(".")
    if silo_name not in schemas:
        raise PackError(path, f"no schema is declared for silo {silo_name!r}")
    try:
        table = schemas[silo_name].table(table_name)
    except KeyError as error:
        raise PackError(path, f"silo {silo_name!r} has no table {table_name!r}") from error
    declared = {column.name for column in table.columns}

    columns_raw = definition.get("columns")
    if not isinstance(columns_raw, dict) or not columns_raw:
        raise PackError(path, "must declare the columns to set under `columns`")
    where_raw = definition.get("where")
    if not isinstance(where_raw, dict) or not where_raw:
        # Without one the update rewrites every row in the table.
        raise PackError(path, "needs a `where` to say which row to revise")

    built: dict[str, Any] = {}
    for section, raw in (("columns", columns_raw), ("where", where_raw)):
        for key, declaration in raw.items():
            key_path = f"{path}.{section}.{key}"
            if key not in declared:
                raise PackError(key_path, f"table {table_name!r} has no column {key!r}")
            try:
                generator = build(declaration)
            except GeneratorError as error:
                raise PackError(key_path, str(error)) from error
            for reference in sorted(generator.references()):
                _check_event_reference(reference, columns_raw, key_path, subject_columns,
                                       has_subject, emitted_so_far)
            built[f"{section}.{key}"] = generator

    return UpdateEmission(
        silo=silo_name, table=table_name,
        columns={k.split(".", 1)[1]: v for k, v in built.items()
                 if k.startswith("columns.")},
        where={k.split(".", 1)[1]: v for k, v in built.items() if k.startswith("where.")},
    )


def _repeat(value: Any, path: str) -> tuple[int, int]:
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 1:
            raise PackError(path, f"repeat must be at least 1, got {value}")
        return value, value
    if isinstance(value, dict):
        unknown = sorted(set(value) - {"min", "max"})
        if unknown:
            raise PackError(f"{path}.repeat", f"does not understand {unknown}")
        low, high = value.get("min", 1), value.get("max", 1)
        if not isinstance(low, int) or not isinstance(high, int) or low < 1 or low > high:
            raise PackError(f"{path}.repeat", f"min {low!r} and max {high!r} are not a range")
        return low, high
    raise PackError(path, f"repeat must be a number or a min/max range, got {value!r}")


def _check_emission_columns(table: Table, columns: dict, path: str,
                            subject_columns: set[str], has_subject: bool,
                            emitted_so_far: set[str]) -> None:
    declared = {column.name for column in table.columns}
    unknown = sorted(set(columns) - declared)
    if unknown:
        raise PackError(path, f"table {table.name!r} has no column(s) {unknown}")
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
        for reference in sorted(generator.references()):
            _check_event_reference(reference, columns, column_path, subject_columns,
                                   has_subject, emitted_so_far)


def _check_event_reference(reference: str, columns: dict, path: str,
                           subject_columns: set[str], has_subject: bool,
                           emitted_so_far: set[str]) -> None:
    """Every reference must resolve where this emission will run.

    Checked against what will ACTUALLY be available: the subject is
    whatever table the event is `per`, and `emitted` may only name a
    table an EARLIER emission in the same event wrote. A pack referring
    forward to a table emitted later would fail mid-run, which is
    exactly the class of mistake this layer exists to catch first.
    """
    parts = reference.split(".")
    root = parts[0]

    if root == "subject":
        if not has_subject:
            raise PackError(path, f"refers to {reference!r}, but this event has no `per`")
        if len(parts) != 2 or parts[1] not in subject_columns:
            raise PackError(
                path,
                f"refers to {reference!r}, but the subject table has columns "
                f"{sorted(subject_columns)}"
            )
        return

    if root == "picked":
        raise PackError(
            path, f"refers to {reference!r}, but nothing can be picked yet"
        )

    if root == "emitted":
        if len(parts) not in (4, 5):
            raise PackError(
                path,
                f"{reference!r} must name a table, an aggregate and a field, as in "
                f"emitted.shop.sale_items.sum.line_total"
            )
        table = ".".join(parts[1:-2])
        if table not in emitted_so_far:
            # Referring FORWARD to a table emitted later in the same
            # event would fail mid-run with an empty aggregate. Caught
            # here because the order of emissions is known at load.
            raise PackError(
                path,
                f"refers to {table!r}, which no earlier emission in this event "
                f"writes to; emissions so far: {sorted(emitted_so_far) or 'none'}"
            )
        return

    if root == "row":
        name = parts[1] if len(parts) > 1 else reference
    else:
        name = reference
    if name not in columns:
        raise PackError(path, f"refers to {name!r}, which this emission does not declare")


def _load_effect(definition: Any, path: str, schemas: dict[str, Schema],
                 subject_columns: set[str], has_subject: bool,
                 emitted_so_far: set[str]) -> AdjustEffect:
    definition = _require_mapping(definition, path)
    unknown = sorted(set(definition) - {"adjust", "by", "where", "floor"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")

    target = _string(definition, "adjust", path)
    if target.count(".") != 2:
        raise PackError(path, f"{target!r} must be written as silo.table.column")
    silo_name, table_name, column_name = target.split(".")
    if silo_name not in schemas:
        raise PackError(path, f"no schema is declared for silo {silo_name!r}")
    try:
        table = schemas[silo_name].table(table_name)
        column = table.column(column_name)
    except KeyError as error:
        raise PackError(path, str(error)) from error
    if column.type not in _NUMERIC_TYPES:
        raise PackError(
            path,
            f"{target!r} is {column.type.value}, which cannot be adjusted; "
            f"adjustable types are {sorted(t.value for t in _NUMERIC_TYPES)}"
        )

    if "by" not in definition:
        raise PackError(path, "needs a `by` saying how much to add")
    by = _effect_generator(definition["by"], f"{path}.by", subject_columns,
                           has_subject, emitted_so_far)

    where_raw = definition.get("where")
    if not isinstance(where_raw, dict) or not where_raw:
        # Without one, the adjustment moves every row in the table.
        raise PackError(path, "needs a `where` to say which rows to adjust")
    where = {}
    for key, declaration in where_raw.items():
        if key not in {column.name for column in table.columns}:
            raise PackError(f"{path}.where", f"table {table_name!r} has no column {key!r}")
        where[key] = _effect_generator(declaration, f"{path}.where.{key}",
                                       subject_columns, has_subject, emitted_so_far)

    return AdjustEffect(silo=silo_name, table=table_name, column=column_name,
                        by=by, where=where, floor=definition.get("floor"))


def _effect_generator(declaration: Any, path: str, subject_columns: set[str],
                      has_subject: bool, emitted_so_far: set[str]):
    """Build a generator for an effect, checking what it may refer to.

    An effect runs after every emission has finished its rows, so the
    context's own row is EMPTY -- `row` and bare names refer to
    nothing. Only the subject and what has been emitted are available,
    and saying so at load is better than a reference error from inside
    a tick.
    """
    try:
        generator = build(declaration)
    except GeneratorError as error:
        raise PackError(path, str(error)) from error
    for reference in sorted(generator.references()):
        root = reference.split(".")[0]
        if root in {"row", "picked"} or "." not in reference:
            raise PackError(
                path,
                f"cannot refer to {reference!r} in an effect: effects run after every "
                f"emission has finished its row, so only `subject` and `emitted` are "
                f"available"
            )
        _check_event_reference(reference, {}, path, subject_columns, has_subject,
                               emitted_so_far)
    return generator


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
