"""
audit.py  (what a consumer actually did, not what it was allowed to do)

THE QUESTION THIS ANSWERS is the one a client asks before pointing any
tool at their production database: not "could it have damaged
anything" -- the grants answer that -- but "what did it actually try".
Those are different, and the difference is the whole point. A tool
that never issues a DROP and a tool whose DROP was refused look
identical from the outside, and only one of them is reassuring.

So every silo logs every statement with the account that issued it,
and this reads that back. Measured on a live world, a consumer's
refused attempt appears in full:

    ...|reader|probe|LOG:  statement: DROP TABLE "sources"
    ...|reader|probe|ERROR:  must be owner of table sources

THE SHAPE IS TAKEN FROM ELYSIUM'S OWN core/intermediate_layer/
audit.py, which was written for the same purpose one layer up: one
entry per event, structured rather than prose, queryable after the
fact. Its reasoning applies unchanged here -- a paper trail nobody can
query is a log file, not an audit.

WHAT IS NOT TAKEN is the file format. Elysium writes its own JSONL
because it owns the events; here the engines write the log and this
reads it, because a record the simulator produced about itself would
prove nothing about a consumer. The evidence has to come from the
database.

TWO FORMATS, because the engines disagree completely. PostgreSQL
prefixes each line with the account and database and repeats the
statement on error. MariaDB's general log names the account only once,
on the Connect line, and identifies everything after it by a numeric
connection id -- so following who did what means tracking those ids.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: What a statement does, in the terms a client cares about. Coarser
#: than SQL's own categories on purpose: the question is not which
#: keyword it was but whether it could have changed or destroyed
#: something.
READ = "read"
WRITE = "write"
DESTRUCTIVE = "destructive"
SCHEMA = "schema"
ADMIN = "admin"
OTHER = "other"

_KINDS = {
    "SELECT": READ, "SHOW": READ, "WITH": READ, "EXPLAIN": READ,
    "SET": OTHER, "COMMIT": OTHER, "ROLLBACK": OTHER, "BEGIN": OTHER,
    "START": OTHER, "USE": OTHER, "FLUSH": ADMIN,
    "INSERT": WRITE, "UPDATE": WRITE, "REPLACE": WRITE,
    "DELETE": DESTRUCTIVE, "TRUNCATE": DESTRUCTIVE, "DROP": DESTRUCTIVE,
    "ALTER": SCHEMA, "CREATE": SCHEMA, "RENAME": SCHEMA, "COMMENT": SCHEMA,
    "GRANT": ADMIN, "REVOKE": ADMIN, "DO": ADMIN,
}

#: Anything that could change or remove data or structure. The set a
#: client actually asks about, named once so a caller cannot ask the
#: question slightly differently somewhere else.
DANGEROUS = frozenset({WRITE, DESTRUCTIVE, SCHEMA, ADMIN})


@dataclass(frozen=True)
class Statement:
    """One statement, and who issued it."""

    at: datetime | None
    account: str
    database: str
    text: str
    kind: str
    #: Whether the engine refused it. A refused attempt is the most
    #: interesting entry there is: it says the tool tried.
    refused: bool = False

    @property
    def dangerous(self) -> bool:
        return self.kind in DANGEROUS


def classify(text: str) -> str:
    """What a statement does, from its first word.

    Crude by design. A parser would be more precise and would also be a
    second SQL implementation to keep correct, and the question here is
    only which of six buckets a statement falls in.
    """
    stripped = text.strip().lstrip("(").strip()
    first = re.split(r"[\s(;]", stripped, maxsplit=1)[0].upper()
    return _KINDS.get(first, OTHER)


# -- PostgreSQL --------------------------------------------------------

_PG_LINE = re.compile(
    r"^(?P<at>[\d-]+ [\d:.]+ \w+)\|(?P<account>[^|]*)\|(?P<database>[^|]*)\|"
    r"(?P<level>LOG|ERROR|FATAL|STATEMENT):\s+(?P<body>.*)$"
)


def read_postgres_log(path: Path) -> Iterator[Statement]:
    """Statements from a PostgreSQL log written with the prefix we set.

    An error arrives as two lines -- ERROR with the reason, then
    STATEMENT with the text -- and the statement itself was already
    logged when it was received. So a refusal is matched back to the
    statement before it rather than emitted again, or every refused
    attempt would be counted twice.
    """
    if not path.exists():
        return
    pending: list[Statement] = []
    for raw in path.read_text(errors="replace").splitlines():
        match = _PG_LINE.match(raw)
        if match is None:
            continue  # a continuation line of a multi-line statement
        level, body = match.group("level"), match.group("body")
        if level == "LOG" and body.startswith("statement: "):
            text = body[len("statement: "):]
            pending.append(Statement(
                at=_parse_moment(match.group("at")),
                account=match.group("account"),
                database=match.group("database"),
                text=text, kind=classify(text),
            ))
        elif level in ("ERROR", "FATAL") and pending:
            last = pending[-1]
            pending[-1] = Statement(at=last.at, account=last.account,
                                    database=last.database, text=last.text,
                                    kind=last.kind, refused=True)
    yield from pending


def _parse_moment(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.rsplit(" ", 1)[0],
                                 "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
    except ValueError:
        return None


# -- MariaDB -----------------------------------------------------------

#: MariaDB's own command words, listed rather than matched as `\w+`.
#: Not defensiveness: TWO of them are two words -- `Init DB` and
#: `Close stmt` -- and a pattern loose enough to admit those is loose
#: enough to read `Query INSERT` as a command and swallow the first
#: word of the statement. Listing them is the only way to tell the two
#: cases apart.
_MY_COMMANDS = ("Connect", "Query", "Quit", "Init DB", "Prepare", "Execute",
                "Close stmt", "Reset stmt", "Field List", "Statistics",
                "Change user", "Refresh", "Shutdown")

#: Two line shapes, and the timestamp has to be matched precisely
#: rather than as "some non-space". MariaDB stamps only the first line
#: of each second -- `260916 18:49:29      3 Connect ...` -- and leaves
#: the rest to start with whitespace. An optional `\S+` for the stamp
#: let the regex read the HOUR as the connection id on stamped lines,
#: so half of every session was attributed to an id that never
#: connected, and appeared as an account called "unknown".
_MY_LINE = re.compile(
    r"^\s*(?:(?P<at>\d{6}\s+\d{1,2}:\d{2}:\d{2})\s+)?"
    r"(?P<id>\d+)\s+(?P<command>" + "|".join(_MY_COMMANDS) + r")\s*(?P<body>.*)$"
)
_MY_CONNECT = re.compile(r"^(?P<account>[^@]+)@\S+\s+on\s+(?P<database>\S*)")


def read_mariadb_log(path: Path) -> Iterator[Statement]:
    """Statements from MariaDB's general query log.

    The account appears ONCE, on the Connect line, and everything after
    it is identified only by a numeric connection id -- so who did what
    has to be reconstructed by tracking those ids. The simulator's own
    connections appear here too, under the owner account, which is what
    makes it possible to tell them apart from a consumer's.
    """
    if not path.exists():
        return
    accounts: dict[str, tuple[str, str]] = {}
    for raw in path.read_text(errors="replace").splitlines():
        match = _MY_LINE.match(raw.replace("\t", " ").rstrip())
        if match is None:
            continue  # a continuation line of a multi-line statement
        identifier, command, body = (match.group("id"), match.group("command"),
                                     match.group("body").strip())
        if command == "Connect":
            who = _MY_CONNECT.match(body)
            if who:
                accounts[identifier] = (who.group("account"),
                                        who.group("database").rstrip("."))
        elif command == "Query" and body:
            account, database = accounts.get(identifier, ("unknown", ""))
            yield Statement(at=_parse_my_moment(match.group("at")),
                            account=account, database=database,
                            text=body, kind=classify(body))
        elif command == "Quit":
            accounts.pop(identifier, None)


def _parse_my_moment(text: str | None) -> datetime | None:
    """MariaDB's own stamp: two-digit year, then a clock time."""
    if not text:
        return None
    try:
        return datetime.strptime(" ".join(text.split()),
                                 "%y%m%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


# -- asking it things --------------------------------------------------

def read_silo(silo) -> list[Statement]:
    """Every statement a SQL silo has seen, whoever issued it."""
    if silo.kind == "postgresql":
        return list(read_postgres_log(silo.log_path))
    if silo.kind == "mariadb":
        return list(read_mariadb_log(silo.query_log_path))
    return []


#: The accounts the simulator itself uses. Every statement the
#: simulation makes is a write, so an audit that counted them would
#: bury the one line an operator is looking for under thousands.
OWNERS = frozenset({"sim", "root", "postgres", "mysql", "mariadb.sys", "unknown"})


def consumers_only(statements: list[Statement]) -> list[Statement]:
    """Just the accounts a consumer was given."""
    return [s for s in statements if s.account not in OWNERS]


def summarise(statements: list[Statement]) -> dict[str, dict[str, int]]:
    """How many statements of each kind, per account.

    The shape an operator wants at a glance: one row per account, and a
    non-zero count under anything but `read` is the thing to look at.
    """
    counts: dict[str, dict[str, int]] = {}
    for statement in statements:
        row = counts.setdefault(statement.account, {})
        row[statement.kind] = row.get(statement.kind, 0) + 1
        if statement.refused:
            row["refused"] = row.get("refused", 0) + 1
    return counts


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: the engines write the log and this reads it, rather than the
# simulator recording what it thinks happened. A record the simulator produced
# about itself would prove nothing about a consumer -- the evidence has to come
# from the database, which is also where it comes from in production.
#
# RESOLVED: the shape is taken from Elysium's core/intermediate_layer/audit.py
# -- one structured entry per event, queryable after the fact -- and the file
# format is not, because Elysium owns its own events and this does not.
#
# RESOLVED, and nearly deleted by mistake: the command word is matched against a
# LIST rather than as `\w+`. Its control did not fire, so it looked speculative
# and was removed -- and four tests failed at once. The justification written at
# the time was wrong (it claimed to guard against continuation lines), which is
# why the control aimed at nothing; the real reason is that `Init DB` and
# `Close stmt` are two words, and any pattern admitting those also reads
# `Query INSERT` as a command and eats the first word of the statement.
# A control that does not fire means the test is weak OR the code is
# speculative, and assuming the second without checking the first nearly
# removed something load-bearing.
#
# RESOLVED: classify() reads the first word instead of parsing. A parser would
# be more precise and would also be a second SQL implementation to keep
# correct, and the question is only which of six buckets a statement is in.
#
# DEFERRED (known, intentional, not yet built): the logs grow without bound. A
# world running a simulated year logs every statement the simulator issues as
# well as every one a consumer does, and nothing rotates or truncates them.
# Rotation is easy; deciding what may be discarded is not, since the point of
# the record is that it is complete.
#
# DEFERRED: no way to tell two consumers apart. They would share the `reader`
# account and appear as one. Per-consumer accounts would fix it and need a way
# to ask for one.
#
# DEFERRED: MariaDB's general log has no timestamp on most lines -- only the
# first of a burst carries one -- so `at` is None for most entries. Ordering is
# still exact; the moment is not. PostgreSQL stamps every line.
