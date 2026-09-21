"""
exports.py  (handing rows to something that is not a database)

A business does not keep everything in one place. It drops a CSV in a
folder for the bookkeeper and serves a JSON feed to whatever the
accountant uses, and both are fed FROM the tables rather than written
alongside them. That is what these two emissions do: read a source
table, and put some of it somewhere a database is not.

Both declare their columns, rather than exporting everything. A feed
that widens whenever somebody adds a column to a table is a feed
nobody can rely on, and the column a firm does not want its bookkeeper
reading is exactly the one an export-everything rule would send.

A WINDOW, NOT A WATERMARK. `since: {column: earned_on, window: 7d}`
reaches back a declared span from now, which is what a real periodic
export is -- nobody computes a bookmark, they run the report for the
period. It needs nothing remembered between runs, and two exports that
overlap or a run that skips a week then behave the way the real thing
would. Before it existed, a simulated year rewrote every early row
fifty-one times.
"""


from simulator.event import ExposeEmission, PublishEmission, Window
from simulator.generators import GeneratorError, build
from simulator.schema import ColumnType, Table
from simulator.spec.references import _check_event_reference
from simulator.spec.scope import EventContext
from simulator.spec.values import PackError, _duration, _string


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
