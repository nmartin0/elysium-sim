"""
references.py  (what a row is built from, shared by seeding and events)

Four things a seed step and an emission both need, and which were in
the events section because that is where they were written rather than
because they belong to it. Extracting seed found them: it could not
move without them, which is the useful signal that they were never
event-specific.

WHAT THEY HAVE IN COMMON is the question "where does this row's
content come from". A row can name the subject it happens to -- the
customer a job is for -- and it can name a row PICKED from somewhere
else, which is what a sale line needs to keep its sku and its price
agreeing about which product they describe. Both are checked the same
way in both places, and two copies of that checking would be two
places to forget a case.

_check_event_reference lives here for the same reason one step further
on: exports, emissions and effects all call it, and it was in the
events section only because that is where the first caller was.
"""

from typing import Any

from simulator.context import AGGREGATES
from simulator.generators import GeneratorError, build
from simulator.schema import Schema, Table
from simulator.spec.scope import EventContext
from simulator.spec.values import PackError


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

def _load_picks(raw: Any, path: str,
                context: EventContext) -> tuple[dict[str, str], EventContext]:
    """Tables this emission chooses a row from before building each row.

    Declared above the columns rather than inside one, because a
    generator returns a single value: two pick generators in the same
    row would choose two different products, and a sale line needs its
    sku and its unit price to come from the same one.
    """
    if raw is None:
        return {}, context
    if not isinstance(raw, list) or not raw:
        raise PackError(f"{path}.picks", "must be a list of silo.table names")

    picks: dict[str, str] = {}
    columns: dict[str, set[str]] = {}
    for qualified in raw:
        if not isinstance(qualified, str) or qualified.count(".") != 1:
            raise PackError(f"{path}.picks",
                            f"{qualified!r} must be written as silo.table")
        silo_name, table_name = qualified.split(".")
        if silo_name not in context.schemas:
            raise PackError(f"{path}.picks",
                            f"no schema is declared for silo {silo_name!r}")
        try:
            table = context.schemas[silo_name].table(table_name)
        except KeyError as error:
            raise PackError(f"{path}.picks",
                            f"silo {silo_name!r} has no table {table_name!r}") from error
        if table_name in picks:
            # Referred to by the bare table name, so two tables sharing
            # one across silos would silently shadow each other.
            raise PackError(
                f"{path}.picks",
                f"two tables called {table_name!r} are picked from; a pack refers to "
                f"a pick by its bare table name, so they would shadow each other"
            )
        picks[table_name] = qualified
        columns[table_name] = {column.name for column in table.columns}
    return picks, context.picking(columns)

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
        # THE AGGREGATE, which went unchecked until a pack wrote
        # `emitted.web.orders.first.order_id` and loaded cleanly, then
        # failed on the first tick of a forty-day run. Every other part
        # of this reference was validated and the one in the middle was
        # not, which is the kind of gap that only shows up when
        # somebody writes a plausible word that happens to be wrong.
        aggregate = parts[-2]
        if aggregate not in AGGREGATES:
            raise PackError(
                path,
                f"{reference!r} asks for {aggregate!r}, which is not an aggregate; "
                f"available: {sorted(AGGREGATES)}"
            )
        return

    if root == "row":
        name = parts[1] if len(parts) > 1 else reference
    else:
        name = reference
    if name not in columns:
        raise PackError(path, f"refers to {name!r}, which this emission does not declare")


def _check_columns(table: Table, columns: dict, path: str, context: EventContext,
                   check_references: Any) -> None:
    """Every column a pack declares for a table it is writing.

    Shared by seed steps and emissions, which were 92% identical --
    same unknown-column check, same non-null check, same generator
    build, differing only in which references are allowed. So that is
    the argument: a seed step may refer to the row and its subject, an
    emission may also refer to what has been emitted and picked.
    """
    declared = {column.name for column in table.columns}
    unknown = sorted(set(columns) - declared)
    if unknown:
        raise PackError(path, f"table {table.name!r} has no column(s) {unknown}")

    # Every column that cannot be null must be generated. A pack that
    # omits one produces rows the engine rejects, which surfaces as a
    # database error mid-run rather than as a pack problem.
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
        check_references(generator.references(), columns, column_path, context)

def _check_picked_reference(reference: str, path: str, context: EventContext) -> None:
    """A `picked.<name>.<column>` reference, wherever it appears.

    Shared by seed steps and emissions because the rule is the same in
    both, and two copies of it would be two places to forget a case.
    """
    parts = reference.split(".")
    if len(parts) != 3:
        raise PackError(
            path,
            f"{reference!r} must name a pick and a column, as in "
            f"picked.products.unit_price"
        )
    available = context.picked_columns(parts[1])
    if available is None:
        raise PackError(
            path,
            f"refers to {reference!r}, but this does not pick from {parts[1]!r}; "
            f"it picks from {sorted(name for name, _ in context.picked) or 'nothing'}"
        )
    if parts[2] not in available:
        raise PackError(
            path,
            f"refers to {reference!r}, but {parts[1]!r} has columns {sorted(available)}"
        )
