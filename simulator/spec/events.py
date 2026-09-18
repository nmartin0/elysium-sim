"""
events.py  (what happens, how often, and what it writes)

The largest section and the last out, because everything else it needs
came out first: the scope a reference is checked against, the shared
machinery for picks and subjects, the exports, the effects.

AN EVENT FIRES EXACTLY ONE WAY. `rate_per_hour`, or `lifecycle` and
`entering`, or `every` -- never two. Arrivals, a state being reached
and a clock striking are three different ideas, and a pack that
declared two would be ambiguous about which it meant while looking
perfectly reasonable. Refusing the combination is the only way to keep
the question "why did this fire" answerable.

EMISSIONS ARE ORDERED AND THE ORDER IS MEANING. An emission can refer
to what an EARLIER one in the same event wrote -- a sale's total from
the lines it just made -- and not to a later one, which is checked
when the pack is read rather than discovered as a missing key three
hours into a backfill.

A transition trigger's subject is the transition itself plus the whole
row that moved, with the transition's own facts winning where the
names collide: a table with a column called `state` must not shadow
the state the entity just entered.
"""

from typing import Any

from simulator.event import (
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
from simulator.scheduler import FLAT
from simulator.schema import Table
from simulator.spec.effects import _load_effect
from simulator.spec.exports import _load_expose, _load_publish
from simulator.spec.references import (
    _check_columns,
    _check_event_reference,
    _load_picks,
    _subject_columns,
)
from simulator.spec.scope import EventContext, LoadContext
from simulator.spec.values import PackError, _duration, _require_mapping, _string


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
