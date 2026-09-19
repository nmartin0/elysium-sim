"""
lifecycles.py  (the arc a thing takes, and where its state is written)

A lifecycle is a state machine an entity walks: requested, quoted,
approved, completed, invoiced, paid. What makes this worth its own
module rather than a few lines of parsing is the checking, and the
checks are about the shape of the machine rather than the syntax of
the file -- a transition to a state nobody declared is a job that
vanishes at run time, and a dwell that is not a duration is a rule
nobody can read.

PERSISTENCE IS DECLARED HERE TOO, and it is what makes a lifecycle
real rather than a diagram: `persisted_to` names a table and a column
where the state actually lives, so a work order's `status` is a thing
a consumer can read and a thing this simulator can pick back up after
a restart.
"""

from typing import Any

from simulator.lifecycle import Lifecycle, Transition
from simulator.schema import ColumnType, Schema
from simulator.spec.model import LifecyclePersistence
from simulator.spec.values import (
    PackError,
    _duration,
    _require_mapping,
    _string,
)


def _load_lifecycles(raw: dict) -> dict[str, Lifecycle]:
    lifecycles = {}
    for name, definition in raw.items():
        path = f"lifecycles.{name}"
        definition = _require_mapping(definition, path)
        unknown = sorted(set(definition) - {"initial", "states", "persisted_to",
                                            "state_column", "entered_column"})
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

def _load_persistence(raw: dict, schemas: dict[str, Schema]) -> dict[str, LifecyclePersistence]:
    persistence = {}
    for name, definition in raw.items():
        path = f"lifecycles.{name}"
        if "persisted_to" not in definition:
            if "entered_column" in definition:
                raise PackError(
                    path,
                    "declares an entered_column but no persisted_to table to write "
                    "it to"
                )
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
        entered = definition.get("entered_column")
        if entered is not None:
            if not isinstance(entered, str):
                raise PackError(path, "entered_column must be a string")
            moment = next((c for c in table.columns if c.name == entered), None)
            if moment is None:
                raise PackError(path, f"table {table_name!r} has no column {entered!r}")
            # No check that this differs from the state column: a
            # state column must be TEXT and this must be a TIMESTAMP,
            # so naming the same column is already refused by the line
            # below. A guard for it was written, could never fire, and
            # was removed -- speculative code with a test that passed
            # for a reason other than the one it named.
            if moment.type is not ColumnType.TIMESTAMP:
                raise PackError(
                    path,
                    f"{entered!r} is {moment.type.value}; an entered_column records a "
                    f"moment and must be a timestamp"
                )

        persistence[name] = LifecyclePersistence(
            silo=silo_name, table=table_name,
            id_column=key.name, state_column=state_column,
            entered_column=entered,
        )
    return persistence
