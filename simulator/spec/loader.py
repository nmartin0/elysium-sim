"""
loader.py  (reading a pack file, and refusing a wrong one)

Everything is checked here, before anything runs. That is the whole
value of this file. A pack naming a column its table does not declare,
a lifecycle transition to a state that was never defined, a curve with
twenty-three hours, a generator that does not exist -- every one of
those is a typo, and the useful place to say so is when the file is
read, with the file and the path named, rather than three hours into a
backfill from inside a tick.

Errors carry their location. `PackError` takes a path like
`schemas.dispatch.tables.customers.columns.balance` because a message
saying "precision is required for DECIMAL" is useless in a file with
eighty columns. This costs a parameter on every helper and is worth it.

Validation lives with loading rather than in its own module. They are
the same operation: this file has no notion of a parsed-but-unchecked
pack, because such a thing has no legitimate use. Splitting them would
create one, and something would eventually consume it.

Keys are checked for being strings at all, which sounds paranoid and
is not. PyYAML follows YAML 1.1, where several bare words are not
strings: `null`, `on`, `off`, `yes` and `no` parse as None, True,
True, False and False. A pack writing `null: false` under a column --
which reads exactly like DDL and is the obvious thing to write -- gets
a mapping keyed by None, and the resulting complaint names a key the
author cannot find in their file. So the nullability key is spelled
`nullable`, and _require_mapping explains the trap rather than letting
it confuse someone.

References are checked against WHERE they will be evaluated. A seed
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
    Window,
)
from simulator.generators import GeneratorError, build
from simulator.scheduler import FLAT
from simulator.schema import ColumnType, Table
from simulator.silos import SILO_TYPES
from simulator.spec.curves import _load_curves
from simulator.spec.lifecycles import _load_lifecycles, _load_persistence
from simulator.spec.migrations import _load_migrations
from simulator.spec.model import (
    PackSpec,
    SiloSpec,
)
from simulator.spec.references import _check_columns, _check_picked_reference, _load_picks, _subject_columns
from simulator.spec.schemas import (
    _load_schemas,
    _relational_kinds,
)
from simulator.spec.scope import EventContext, LoadContext
from simulator.spec.seed import _load_seed
from simulator.spec.values import (
    PackError,
    _duration,
    _mapping,
    _require_mapping,
    _string,
)

#: Column types an effect may adjust. Adjusting text or a date is not
#: a thing a business does, and silently producing SQL the engine
#: rejects would be worse than refusing the pack.
_NUMERIC_TYPES = frozenset({ColumnType.INTEGER, ColumnType.BIGINT, ColumnType.DECIMAL})

#: Silo kinds that can hold a schema. Derived from the dialects that
#: exist rather than listed again: a kind with no dialect cannot have
#: tables created in it, and saying so twice is how the two lists drift.


#: Suffixes accepted in a duration like `4h` or `30m`. Durations appear
#: as min_dwell on lifecycle transitions and read far better than a
#: count of seconds -- `min_dwell: 7d` against `min_dwell: 604800`.

#: The namespaces a generator may reference during seeding. A seed step
#: has no subject, nothing picked and nothing emitted, so only the row
#: being built is available.


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
    seed_context = EventContext(pack=LoadContext(
        schemas=schemas, silos=silos, curves=curves,
        lifecycles=lifecycles, persistence=persistence))
    seed = _load_seed(raw.get("seed") or [], seed_context)
    events = _load_events(raw.get("events") or {}, seed_context.pack)

    migrations = _load_migrations(raw.get("migrations") or [], schemas)

    return PackSpec(name=name, description=description, silos=silos,
                    schemas=schemas, curves=curves, lifecycles=lifecycles,
                    persistence=persistence, seed=seed, events=events,
                    migrations=migrations)


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


# -- schemas ---------------------------------------------------------


# -- lifecycles ------------------------------------------------------


# -- seed ------------------------------------------------------------


# -- events ----------------------------------------------------------

def _load_events(raw: Any, context: LoadContext) -> tuple[Event, ...]:
    if not isinstance(raw, dict):
        raise PackError("events", "must be a mapping of event name to declaration")
    return tuple(
        _load_event(name, definition, f"events.{name}", context)
        for name, definition in raw.items()
    )


def _load_event(name: str, definition: Any, path: str, context: LoadContext) -> Event:
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
                             path, EventContext(pack=context))

    subject_columns: set[str] = set()
    per = definition.get("per")

    if by_transition:
        trigger, subject_columns = _transition_trigger(definition, path, context)
        if per is not None:
            raise PackError(
                path, "a transition event happens to the entity that moved, not to a `per`"
            )
        return _finish_event(name, trigger, definition, path,
                             EventContext(pack=context).about(subject_columns))

    rate = definition["rate_per_hour"]
    if not isinstance(rate, int | float) or isinstance(rate, bool) or rate <= 0:
        raise PackError(path, f"rate_per_hour must be a positive number, got {rate!r}")

    if per is not None:
        if not isinstance(per, str):
            raise PackError(path, "per must be written as silo.table")
        subject_columns = _subject_columns(per, f"{path}.per", context.schemas)

    curve_name = definition.get("curve")
    if curve_name is not None and curve_name not in context.curves:
        raise PackError(
            f"{path}.curve",
            f"no curve called {curve_name!r}; this pack declares {sorted(context.curves)}"
        )

    return _finish_event(
        name,
        RateTrigger(rate_per_hour=float(rate), per=per,
                    curve=context.curves[curve_name] if curve_name else FLAT,
                    stream=f"event.{name}"),
        definition, path, EventContext(pack=context).about(subject_columns),
    )


def _transition_trigger(definition: dict, path: str,
                        context: LoadContext) -> tuple[TransitionTrigger, set[str]]:
    lifecycle_name = _string(definition, "lifecycle", path)
    if lifecycle_name not in context.lifecycles:
        raise PackError(
            path, f"no lifecycle called {lifecycle_name!r}; "
                  f"this pack declares {sorted(context.lifecycles)}"
        )
    entering = _string(definition, "entering", path)
    states = context.lifecycles[lifecycle_name].states
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

    where = context.persistence.get(lifecycle_name)
    subject_columns = {"state", "previous_state",
                       where.id_column if where else "entity_id"}
    if where is not None:
        # The entity's own row travels with the transition, so an event
        # can reach every column of it -- which is what lets an invoice
        # know whose it is.
        subject_columns |= {
            column.name
            for column in context.schemas[where.silo].table(where.table).columns}
    return TransitionTrigger(lifecycle=lifecycle_name, entering=entering), subject_columns


def _finish_event(name: str, trigger, definition: dict, path: str,
                  context: EventContext) -> Event:
    """The half that is the same however an event is triggered."""
    emits = definition.get("emits")
    if not isinstance(emits, list) or not emits:
        raise PackError(path, "must declare at least one emission under `emits`")

    emissions = []
    for index, emit_def in enumerate(emits):
        emission = _load_emission(emit_def, f"{path}.emits[{index}]", context)
        emissions.append(emission)
        # A new context rather than a mutated set, so "what has been
        # emitted so far" is a value at each point rather than a
        # variable whose history has to be reasoned about.
        context = context.having_emitted(emission.qualified)

    effects = tuple(
        _load_effect(effect_def, f"{path}.effects[{index}]", context)
        for index, effect_def in enumerate(definition.get("effects") or [])
    )
    return Event(name=name, trigger=trigger, emissions=tuple(emissions), effects=effects)


def _load_emission(definition: Any, path: str, context: EventContext,
                   ) -> InsertEmission | UpdateEmission | PublishEmission | ExposeEmission:
    definition = _require_mapping(definition, path)
    if "publish" in definition:
        return _load_publish(definition, path, context)
    if "expose" in definition:
        return _load_expose(definition, path, context)
    if "update" in definition:
        return _load_update(definition, path, context)
    unknown = sorted(set(definition) - {"table", "columns", "repeat", "spawns",
                                        "picks"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")

    qualified = _string(definition, "table", path)
    if qualified.count(".") != 1:
        raise PackError(path, f"table {qualified!r} must be written as silo.table")
    silo_name, table_name = qualified.split(".")
    if silo_name not in context.schemas:
        raise PackError(path, f"no schema is declared for silo {silo_name!r}")
    try:
        table = context.schemas[silo_name].table(table_name)
    except KeyError as error:
        raise PackError(path, f"silo {silo_name!r} has no table {table_name!r}") from error

    low, high = _repeat(definition.get("repeat", 1), path)

    columns = definition.get("columns")
    if not isinstance(columns, dict) or not columns:
        raise PackError(path, "must declare column generators under `columns`")
    picks, context = _load_picks(definition.get("picks"), path, context)
    _check_emission_columns(table, columns, path, context)

    spawns = definition.get("spawns")
    key_column = None
    if spawns is not None:
        if spawns not in context.pack.lifecycles:
            raise PackError(
                path,
                f"no lifecycle called {spawns!r}; this pack declares "
                f"{sorted(context.pack.lifecycles)}"
            )
        where = context.pack.persistence.get(spawns)
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
        picks=picks,
    )


def _load_expose(definition: dict, path: str, context: EventContext) -> ExposeEmission:
    """An emission that publishes a collection through a REST silo."""
    unknown = sorted(set(definition) - {"expose", "collection", "rows_from",
                                        "columns", "since"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")

    target = _string(definition, "expose", path)
    if target not in context.pack.silos:
        raise PackError(path, f"there is no silo called {target!r}")
    if context.pack.silos[target].kind != "rest":
        raise PackError(
            path,
            f"silo {target!r} is a {context.pack.silos[target].kind!r} silo; exposing "
            f"a collection needs a rest silo"
        )

    collection = _string(definition, "collection", path)
    if not collection.replace("_", "").isalnum():
        # It becomes a URL path segment, so it has to survive being one.
        raise PackError(
            f"{path}.collection",
            f"{collection!r} becomes a URL path segment and must be alphanumeric "
            f"or underscored"
        )

    source_silo, source_table, columns = _source_rows(definition, path, context)
    window = _load_window(definition, path,
                          context.schemas[source_silo].table(source_table))
    return ExposeEmission(silo=target, collection=collection, source_silo=source_silo,
                          source_table=source_table, columns=columns,
                          window=window)


def _load_window(definition: dict, path: str, table: Table) -> "Window | None":
    """How far back an export reaches, if it does not reach all the way.

    A window rather than a watermark: "last week's transactions" is a
    function of the clock and a declared span, which is what a real
    periodic export is. Nothing has to be remembered between runs.
    """
    raw = definition.get("since")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"column", "window"}:
        raise PackError(
            f"{path}.since", "needs exactly `column` and `window`, as in "
            "{column: earned_on, window: 7d}")
    column_name = str(raw["column"])
    try:
        column = table.column(column_name)
    except KeyError as error:
        raise PackError(f"{path}.since", str(error)) from error
    if column.type not in (ColumnType.DATE, ColumnType.TIMESTAMP):
        raise PackError(
            f"{path}.since",
            f"{column_name!r} is {column.type.value}; a window needs a date or a "
            f"timestamp to measure from"
        )
    seconds = _duration(raw["window"], f"{path}.since.window")
    if seconds <= 0:
        raise PackError(f"{path}.since.window", "must be a positive interval")
    return Window(column=column_name, seconds=seconds)


def _source_rows(definition: dict, path: str,
                 context: EventContext) -> tuple[str, str, tuple[str, ...]]:
    """Where an export's rows come from, and which columns it takes.

    Shared by publishing a file and exposing a collection, because the
    question is the same one and answering it twice is how the two
    would eventually disagree about what `rows_from` means.
    """
    source = _string(definition, "rows_from", path)
    if source.count(".") != 1:
        raise PackError(path, f"rows_from {source!r} must be written as silo.table")
    silo_name, table_name = source.split(".")
    if silo_name not in context.schemas:
        raise PackError(path, f"no schema is declared for silo {silo_name!r}")
    try:
        table = context.schemas[silo_name].table(table_name)
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


def _load_publish(definition: dict, path: str, context: EventContext) -> PublishEmission:
    """An emission that writes a file into a file-drop silo."""
    unknown = sorted(set(definition) - {"publish", "filename", "rows_from",
                                        "columns", "since"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")

    target = _string(definition, "publish", path)
    if target not in context.pack.silos:
        raise PackError(path, f"there is no silo called {target!r}")
    if context.pack.silos[target].kind != "filedrop":
        raise PackError(
            path,
            f"silo {target!r} is a {context.pack.silos[target].kind!r} silo; publishing "
            f"a file needs a filedrop silo"
        )

    source_silo, source_table, columns = _source_rows(definition, path, context)
    window = _load_window(definition, path,
                          context.schemas[source_silo].table(source_table))

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
        _check_event_reference(reference, facts, f"{path}.filename", context)

    return PublishEmission(silo=target, filename=filename, source_silo=source_silo,
                           source_table=source_table, columns=columns,
                          window=window)


def _load_update(definition: dict, path: str, context: EventContext) -> UpdateEmission:
    """An emission that revises a row rather than writing one."""
    unknown = sorted(set(definition) - {"update", "columns", "where"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")

    qualified = _string(definition, "update", path)
    if qualified.count(".") != 1:
        raise PackError(path, f"table {qualified!r} must be written as silo.table")
    silo_name, table_name = qualified.split(".")
    if silo_name not in context.schemas:
        raise PackError(path, f"no schema is declared for silo {silo_name!r}")
    try:
        table = context.schemas[silo_name].table(table_name)
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
                _check_event_reference(reference, columns_raw, key_path, context)
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
                            context: EventContext) -> None:
    def check(references, declared, column_path, event_context):
        for reference in sorted(references):
            _check_event_reference(reference, declared, column_path, event_context)

    _check_columns(table, columns, path, context, check)


def _check_event_reference(reference: str, columns: dict, path: str,
                           context: EventContext) -> None:
    """Every reference must resolve where this emission will run.

    Checked against what will actually be available: the subject is
    whatever table the event is `per`, and `emitted` may only name a
    table an earlier emission in the same event wrote. A pack referring
    forward to a table emitted later would fail mid-run, which is
    exactly the class of mistake this layer exists to catch first.
    """
    parts = reference.split(".")
    root = parts[0]

    if root == "subject":
        if not context.has_subject:
            raise PackError(path, f"refers to {reference!r}, but this event has no `per`")
        if len(parts) != 2 or parts[1] not in context.subject_columns:
            raise PackError(
                path,
                f"refers to {reference!r}, but the subject table has columns "
                f"{sorted(context.subject_columns)}"
            )
        return

    if root == "picked":
        _check_picked_reference(reference, path, context)
        return


    if root == "emitted":
        if len(parts) not in (4, 5):
            raise PackError(
                path,
                f"{reference!r} must name a table, an aggregate and a field, as in "
                f"emitted.shop.sale_items.sum.line_total"
            )
        table = ".".join(parts[1:-2])
        if table not in context.emitted:
            # Referring forward to a table emitted later in the same
            # event would fail mid-run with an empty aggregate. Caught
            # here because the order of emissions is known at load.
            raise PackError(
                path,
                f"refers to {table!r}, which no earlier emission in this event "
                f"writes to; emissions so far: {sorted(context.emitted) or 'none'}"
            )
        return

    if root == "row":
        name = parts[1] if len(parts) > 1 else reference
    else:
        name = reference
    if name not in columns:
        raise PackError(path, f"refers to {name!r}, which this emission does not declare")


def _load_effect(definition: Any, path: str, context: EventContext) -> AdjustEffect:
    definition = _require_mapping(definition, path)
    unknown = sorted(set(definition) - {"adjust", "by", "where", "floor"})
    if unknown:
        raise PackError(path, f"does not understand {unknown}")

    target = _string(definition, "adjust", path)
    if target.count(".") != 2:
        raise PackError(path, f"{target!r} must be written as silo.table.column")
    silo_name, table_name, column_name = target.split(".")
    if silo_name not in context.schemas:
        raise PackError(path, f"no schema is declared for silo {silo_name!r}")
    try:
        table = context.schemas[silo_name].table(table_name)
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
    by = _effect_generator(definition["by"], f"{path}.by", context)

    where_raw = definition.get("where")
    if not isinstance(where_raw, dict) or not where_raw:
        # Without one, the adjustment moves every row in the table.
        raise PackError(path, "needs a `where` to say which rows to adjust")
    where = {}
    for key, declaration in where_raw.items():
        if key not in {column.name for column in table.columns}:
            raise PackError(f"{path}.where", f"table {table_name!r} has no column {key!r}")
        where[key] = _effect_generator(declaration, f"{path}.where.{key}", context)

    return AdjustEffect(silo=silo_name, table=table_name, column=column_name,
                        by=by, where=where, floor=definition.get("floor"))


def _effect_generator(declaration: Any, path: str, context: EventContext):
    """Build a generator for an effect, checking what it may refer to.

    An effect runs after every emission has finished its rows, so the
    context's own row is empty -- `row` and bare names refer to
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
        _check_event_reference(reference, {}, path, context)
    return generator


# -- migrations ------------------------------------------------------


#: Every operation a pack may schedule, by the name it uses. Explicit
#: rather than derived from the class names, so the vocabulary a pack
#: writes is a decision rather than an accident of refactoring.
#:
#: Public because the interactive console builds operations from the
#: same words, and a second vocabulary meaning the same things would
#: be the worst of both.


# -- small helpers ---------------------------------------------------


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
# RESOLVED: _relational_kinds() derives from dialects rather than listing kinds
# again. A silo with no dialect cannot have tables created in it, and writing
# that fact in two places is how the two lists drift.
#
# RESOLVED: seed generators are checked against the namespaces available during
# seeding, which is only `row`. A seed step has no subject and nothing emitted,
# so `{from: subject.store_id}` is a pack error catchable at load. This is the
# clearest use of generators.references() and the reason it exists.
#
# DEFERRED (known, intentional, not yet built): a non-null column with a
# database default still has to be generated, because the schema layer has no
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
