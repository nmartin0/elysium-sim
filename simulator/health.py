"""
health.py  (asking a silo whether it is sound, as a consumer would)

WHAT `simulator verify` Actually runs. Somebody learning to connect a
tool to these databases will hit a problem, and their first question
is whether the fault is theirs or the trainer's. Without an answer
they spend the afternoon in the wrong logs.

Every check uses only connections.json -- no world object, no pack, no
privileged account. That is what makes the answer worth anything: a
check that used the simulator's own superuser would pass on a database
no consumer could read.

The checks mirror what A Handover claims, so the document and the
databases cannot drift apart. Reachable, tables present and populated,
every table with a primary key, the read account genuinely unable to
write, the file drop holding complete files with the byte-order mark,
and the API paging to the end rather than stopping at its first page.

One of them deliberately does not fail: a file drop with nothing
published yet is a weekly export that is not due, and calling that
broken would send an engineer hunting a problem that does not exist --
which is the exact failure this command exists to prevent.
"""

from pathlib import Path

from simulator.attaching import reach


def _sql_checks() -> list:
    from simulator.relational import fetch_all, read_schema

    def reachable(name, details, directory):
        silo = reach(name, details, directory)
        if not silo.is_reachable():
            raise RuntimeError("the server is not answering")
        return None

    def tables_have_rows(name, details, directory):
        silo = reach(name, details, directory)
        schema = read_schema(silo, details["database"])
        if not schema.tables:
            raise RuntimeError("the database has no tables in it")
        empty = []
        for table in schema.tables:
            count = fetch_all(silo, details["database"],
                              f"SELECT count(*) FROM {_quoted(details, table.name)}")[0][0]
            if count == 0:
                empty.append(table.name)
        if empty:
            raise RuntimeError(f"these tables are empty: {sorted(empty)}")
        return f"{len(schema.tables)} tables, all populated"

    def every_table_has_a_key(name, details, directory):
        silo = reach(name, details, directory)
        scope = "public" if details["kind"] == "postgresql" else details["database"]
        # key_column_usage, not table_constraints: the latter comes back
        # empty for an account holding only SELECT, on both engines.
        keyed = {str(row[0]) for row in fetch_all(
            silo, details["database"],
            "SELECT DISTINCT table_name FROM information_schema.key_column_usage "
            "WHERE table_schema = %s", (scope,))}
        missing = [t.name for t in read_schema(silo, details["database"]).tables
                   if t.name not in keyed]
        if missing:
            raise RuntimeError(f"no primary key on {sorted(missing)}")
        return None

    def the_reader_cannot_write(name, details, directory):
        silo = reach(name, details, directory)
        table = read_schema(silo, details["database"]).tables[0].name
        # Through the PUBLISHED account, not the simulator's own, or
        # this would prove nothing about what was handed over.
        import psycopg
        import pymysql

        drivers = {"postgresql": psycopg, "mariadb": pymysql}
        driver = drivers[details["kind"]]
        # The password too, from the published details. `verify` is a
        # consumer and connects like one; when the databases started
        # requiring a credential this was one of the things that had to
        # learn to send it, which is the point of requiring it.
        named = "dbname" if details["kind"] == "postgresql" else "database"
        kwargs = {"host": details["host"], "port": details["port"],
                  named: details["database"], "user": details["user"],
                  "password": details["password"]}
        connection = driver.connect(**kwargs)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {_quoted(details, table)}")
        except Exception as error:
            # The reason matters. Any exception used to count as
            # "refused, as it should be" -- so a missing table, a
            # dropped connection or a typo in the statement reported a
            # security guarantee that had not been tested at all. A
            # check that can pass for the wrong reason is worse than no
            # check, because somebody believes it.
            if not _is_permission_error(error):
                raise RuntimeError(
                    f"the DELETE failed, but not because it was refused: {error}"
                ) from error
            return "DELETE refused, as it should be"
        finally:
            connection.close()
        raise RuntimeError(f"the {details['user']!r} account was allowed to DELETE")

    return [("reachable", reachable),
            ("tables present and populated", tables_have_rows),
            ("every table has a primary key", every_table_has_a_key),
            ("the read account cannot write", the_reader_cannot_write)]

#: What each engine says when it refuses for want of privilege.
#: Matched on the message because the driver exception types differ and
#: both engines are reached through their own driver -- psycopg raises
#: InsufficientPrivilege, pymysql an OperationalError with code 1142,
#: and a shared check on the text is the smaller of the two evils.
_REFUSAL_SIGNS = ("permission denied", "must be owner", "insufficient privilege",
                  "access denied", "command denied", "1142", "1044", "1045")


def _is_permission_error(error: Exception) -> bool:
    """Whether the database refused this, rather than failing at it."""
    return any(sign in str(error).lower() for sign in _REFUSAL_SIGNS)


def _quoted(details: dict, name: str) -> str:
    return f'"{name}"' if details["kind"] == "postgresql" else f"`{name}`"

def _filedrop_checks() -> list:
    def files_are_complete(name, details, directory):
        folder = Path(details["path"])
        if not folder.exists():
            raise RuntimeError(f"{folder} is not there")
        partial = list(folder.glob("*.part"))
        if partial:
            raise RuntimeError(f"half-written files present: {[p.name for p in partial]}")
        files = sorted(folder.glob("*.csv"))
        if not files:
            # NOT a fault. A weekly export that is not due yet has
            # published nothing, and telling an engineer their trainer
            # is broken because of it would send them hunting a problem
            # that does not exist -- which is the exact failure this
            # command is meant to prevent.
            return "nothing published yet, which is fine if none is due"
        return f"{len(files)} files, none half-written"

    def the_encoding_is_as_advertised(name, details, directory):
        files = sorted(Path(details["path"]).glob("*.csv"))
        if not files:
            return "nothing to check yet"
        raw = files[0].read_bytes()
        if details.get("encoding") == "utf-8-sig" and not raw.startswith(b"\xef\xbb\xbf"):
            raise RuntimeError("advertised as utf-8-sig but the byte-order mark is missing")
        return str(details.get("encoding"))

    return [("files complete", files_are_complete),
            ("encoding as advertised", the_encoding_is_as_advertised)]

def _rest_checks() -> list:
    import json as _json
    import urllib.request

    def ask(details, path):
        request = urllib.request.Request(str(details["base_url"]) + path)
        request.add_header("Authorization", f"Bearer {details['token']}")
        with urllib.request.urlopen(request, timeout=5) as response:
            return _json.loads(response.read())

    def answering(name, details, directory):
        body = ask(details, "/v1/invoices")
        if not body.get("data"):
            raise RuntimeError("the feed is empty")
        return f"{len(body['data'])} records on the first page"

    def paging_works(name, details, directory):
        seen, path = 0, "/v1/invoices"
        for _ in range(200):
            body = ask(details, path)
            seen += len(body.get("data", []))
            if "cursor" not in body:
                return f"{seen} records across every page"
            path = f"/v1/invoices?cursor={body['cursor']}"
        raise RuntimeError("the cursor never ran out, which means it is not advancing")

    return [("answering", answering), ("pages to the end", paging_works)]

_CHECKS = {
    "postgresql": _sql_checks(),
    "mariadb": _sql_checks(),
    "filedrop": _filedrop_checks(),
    "rest": _rest_checks(),
}
