"""
seed.py  (the reference data a business already has)

Customers, engineers, tills, skills: the rows that exist before
anything happens. A seed step writes a fixed number of them, or one
per row of another table, or SEVERAL per row -- which is what a join
table needs, since a technician has many skills and not one.

WHAT IT CHECKS is where a generator may look. Nothing has been emitted
during seeding, so that namespace is a mistake and a detectable one,
because generators report what they depend on. `subject` is available
only to a step that declared a `per`, and `picked` only to one that
declared what it picks from -- which a join table does, since a row
pairing a technician with a skill has to name a skill from somewhere.

The whole pass is a demonstration of why Generator.references() exists:
without it a pack referring to a column that will not be there fails
somewhere in the middle of a run, with a message about a missing key
and no indication which line of which file is wrong.
"""

from typing import Any

from simulator.schema import Table
from simulator.spec.model import SeedStep
from simulator.spec.references import (
    _check_columns,
    _check_picked_reference,
    _load_picks,
    _subject_columns,
)
from simulator.spec.scope import EventContext
from simulator.spec.values import (
    PackError,
    _check_keys_are_strings,
    _require_mapping,
    _string,
)


def _load_seed(raw: Any, context: EventContext) -> tuple[SeedStep, ...]:
    if not isinstance(raw, list):
        raise PackError("seed", "must be a list of steps")
    return tuple(
        _load_seed_step(step, f"seed[{index}]", context)
        for index, step in enumerate(raw)
    )

def _load_seed_step(definition: Any, path: str, context: EventContext) -> SeedStep:
    definition = _require_mapping(definition, path)
    unknown = sorted(set(definition) - {"table", "count", "columns", "per", "picks",
                                        "rows", "distinct_picks"})
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

    if "rows" in definition:
        return _load_literal_rows(definition, path, silo_name, table_name, table)

    per = definition.get("per")
    subject_columns: set[str] = set()
    if per is not None:
        # `count` alongside `per` means rows PER SUBJECT. It used to be
        # refused, which made a join table inexpressible: a technician
        # has several skills, not one.
        if not isinstance(per, str):
            raise PackError(path, "per must be written as silo.table")
        subject_columns = _subject_columns(per, f"{path}.per", context.schemas)

    count = definition.get("count", 1)
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise PackError(path, f"count must be a positive whole number, got {count!r}")

    columns = definition.get("columns")
    if not isinstance(columns, dict) or not columns:
        raise PackError(path, "must declare column generators under `columns`")

    picks, context = _load_picks(definition.get("picks"), path, context)
    _check_seed_columns(table, columns, path, context.about(subject_columns))
    distinct = definition.get("distinct_picks", False)
    if not isinstance(distinct, bool):
        raise PackError(path, "distinct_picks must be true or false")
    if distinct and not picks:
        raise PackError(path, "distinct_picks needs something to pick from")
    if distinct and per is None:
        # Without `per` there is one row per step and nothing to be
        # distinct FROM, so asking for it means the pack meant
        # something else.
        raise PackError(
            path, "distinct_picks applies across a subject's rows, so it needs `per`")

    return SeedStep(silo=silo_name, table=table_name, count=count, per=per,
                    columns=dict(columns), picks=picks, rows=(),
                    distinct_picks=distinct)

def _load_literal_rows(definition: dict, path: str, silo_name: str,
                       table_name: str, table: Table) -> SeedStep:
    """A lookup table, written out rather than generated.

    Skills, branches, statuses, categories: a fixed handful of rows a
    business simply HAS, where the values are the point. No generator
    can express that -- `choice` draws with replacement, so six draws
    from six options gave two skills named the same and none named
    several of the others, which reads as corrupt reference data rather
    than as generated data.

    EVERY ROW DECLARES THE SAME COLUMNS, checked here rather than left
    to the insert. A row missing a key is a NULL the table may not
    allow, and finding that out from a driver error names the database
    rather than the line of YAML that is wrong.
    """
    unusable = sorted(set(definition) & {"count", "per", "picks", "columns"})
    if unusable:
        raise PackError(
            path,
            f"a step with `rows` writes exactly those rows, so it cannot also take "
            f"{unusable}"
        )
    raw = definition["rows"]
    if not isinstance(raw, list) or not raw:
        raise PackError(path, "rows must be a non-empty list of mappings")

    declared = {column.name for column in table.columns}
    required = {column.name for column in table.columns if not column.nullable}
    rows = []
    for index, row in enumerate(raw):
        where = f"{path}.rows[{index}]"
        row = _require_mapping(row, where)
        _check_keys_are_strings(row, where)
        unknown = sorted(set(row) - declared)
        if unknown:
            raise PackError(where, f"table {table_name!r} has no column(s) {unknown}")
        missing = sorted(required - set(row))
        if missing:
            raise PackError(
                where,
                f"leaves out {missing}, which {table_name!r} declares NOT NULL"
            )
        rows.append(dict(row))

    keys = {frozenset(row) for row in rows}
    if len(keys) > 1:
        # Rows that declare different columns produce a table where
        # some rows have values nobody meant to leave out, which is the
        # kind of thing spotted much later by somebody reading the data.
        raise PackError(
            path,
            f"every row must declare the same columns; got "
            f"{sorted(sorted(k) for k in keys)}"
        )

    return SeedStep(silo=silo_name, table=table_name, count=len(rows), per=None,
                    columns={}, picks={}, rows=tuple(rows), distinct_picks=False)


def _check_seed_columns(table: Table, columns: dict, path: str,
                        context: EventContext) -> None:
    _check_columns(table, columns, path, context, _check_seed_references)

def _check_seed_references(references: set[str], columns: dict, path: str,
                           context: EventContext) -> None:
    """A seed step may refer to the row it is building, its subject, and
    anything it picked.

    Nothing has been EMITTED during seeding, so that namespace is still
    a mistake -- and a detectable one, because generators report what
    they depend on. `picked` was in the same position until a join
    table needed it: a row pairing a technician with a skill has to
    name a skill from somewhere.
    """
    for reference in sorted(references):
        root = reference.split(".")[0]
        if root in _SEED_NAMESPACES:
            continue
        if root == "picked":
            _check_picked_reference(reference, path, context)
            continue
        if root == "subject":
            if not context.has_subject:
                raise PackError(
                    path, f"refers to {reference!r}, but this step has no `per`"
                )
            parts = reference.split(".")
            if len(parts) != 2 or parts[1] not in context.subject_columns:
                raise PackError(
                    path,
                    f"refers to {reference!r}, but the subject table has columns "
                    f"{sorted(context.subject_columns)}"
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

_SEED_NAMESPACES = frozenset({"row"})
