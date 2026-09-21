"""Rules about the tests themselves, checked mechanically.

A module-scoped world is shared by every test in its file, so a test
that changes one has changed what every later test reads. That is a
real hazard and an invisible one: the damage looks like a flaky
assertion in an unrelated test, and only when the order happens to
put them that way.

It was found by hand once already, in tests/consumer -- a write test
set a column that a read test asserted was null -- and by hand is not
good enough for a rule that only bites when somebody adds a test
months later.
"""

import ast
import pathlib

import pytest

TESTS = pathlib.Path(__file__).resolve().parent

#: Things a test does that change the world it was given. Named here
#: because this is the definition the optimisation depends on, and a
#: definition that lives in a comment is one nobody can check against.
MUTATORS = ("runner.run", "runner.tick", "runner.seed", "runner.stop",
            "insert_rows", "update_columns", "set_column", "adjust_column",
            ".apply(", "terminate()", "write_connections")


def module_scoped_fixtures(tree: ast.Module) -> set[str]:
    found = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if "fixture" not in ast.dump(decorator):
                continue
            if 'scope' in ast.dump(decorator) and 'module' in ast.dump(decorator):
                found.add(node.name)
            if 'session' in ast.dump(decorator):
                found.add(node.name)
    return found


def files_with_shared_worlds():
    for path in sorted(TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text())
        shared = module_scoped_fixtures(tree)
        if shared:
            yield path, tree, shared


@pytest.mark.parametrize(
    "path,tree,shared",
    list(files_with_shared_worlds()),
    ids=[p.name for p, _, _ in files_with_shared_worlds()],
)
def test_no_test_mutates_a_world_it_shares(path, tree, shared):
    source = path.read_text()
    offending = []
    for node in tree.body:
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test")):
            continue
        taken = {argument.arg for argument in node.args.args} & shared
        if not taken:
            continue
        body = ast.get_source_segment(source, node) or ""
        used = [m for m in MUTATORS if m in body]
        if used:
            offending.append(f"{node.name} uses {used} on shared {sorted(taken)}")

    assert offending == [], (
        f"{path.name}: these tests change a world they share with every other "
        f"test in the file, so the damage will surface as a flaky assertion "
        f"somewhere else:\n  " + "\n  ".join(offending) +
        "\n\nGive them their own fixture, as test_transitions.py does with "
        "`moving_world`."
    )


def test_there_are_shared_worlds_to_check():
    # A rule that checks nothing would pass forever. Three files share
    # a world today; if that drops to zero, either the optimisation was
    # reverted or this check stopped finding them.
    assert len(list(files_with_shared_worlds())) >= 3


# -- the emphasis tic --------------------------------------------------

#: SQL is legitimately written in capitals inside prose -- "GRANT SELECT
#: ON ALL TABLES", "CREATE USER IF NOT EXISTS" -- so a run made entirely
#: of these is left alone.
_SQL_WORDS = frozenset("""GRANT SELECT ON ALL TABLES IN SCHEMA ALTER DEFAULT
PRIVILEGES FOR ROLE TO CONNECT DATABASE USAGE CREATE USER IF NOT EXISTS THEN
REVOKE TABLE WITH OPTION TIMESTAMP WITHOUT TIME ZONE IS NULL UPDATE INSERT
DELETE DROP TRUNCATE FROM WHERE AND OR BY ORDER GROUP PRIMARY KEY UNIQUE
DECIMAL INTEGER BIGINT TEXT DATE BOOLEAN VALUES INTO SET JOIN AS DISTINCT
COUNT SUM MIN MAX LIMIT OFFSET CHARSET COLLATE IDENTIFIED""".split())

#: The one fixed section marker that is capitalised on purpose. Exempt
#: by the whole line rather than by adding its words to a list: the
#: sweep that preceded this check rewrote it to "AI-Only notes" in all
#: thirty-seven files, and letting ONLY and NOTES through generally
#: would let "EVERY NOTE IS CHECKED ONLY" through with them.
_MARKER = "AI-ONLY NOTES -- not user-facing"


def _shouted_phrases():
    """Multi-word runs of capitals in comments that are not SQL."""
    import re

    root = TESTS.parent / "simulator"
    found = []
    for path in sorted(root.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not line.lstrip().startswith("#") or _MARKER in line:
                continue
            for match in re.finditer(r"\b(?:[A-Z]{2,}\s+){1,}[A-Z]{2,}\b", line):
                if not all(word in _SQL_WORDS for word in match.group(0).split()):
                    found.append(f"{path.name}:{number}: {match.group(0)}")
    return found


def test_comments_are_not_shouted():
    # Why this is a test and not a habit.
    # A sweep took this codebase from 793 shouted words to one. Within
    # twenty-five commits it had come back to 601, with 132 distinct
    # multi-word phrases -- "PROTOCOL BREAKS THE CYCLE BECAUSE NOTHING
    # HAS TO IMPORT ANYTHING" -- and a second sweep was needed.
    #
    # A file where everything is emphasised has nothing emphasised. And
    # a rule that depends on remembering it is a rule that has already
    # failed twice here, so it is checked instead.
    # A first version of this check could not fail. Written through a
    # quoted heredoc, its pattern reached the file as r"\\b..." -- a
    # raw string holding a literal backslash -- which matches no
    # ordinary text, so every comment passed. The negative control
    # aimed at it stayed silent, which is the only reason it was found.
    shouted = _shouted_phrases()
    assert shouted == [], (
        f"{len(shouted)} shouted phrase(s) in comments -- write them as "
        f"ordinary prose:\n  " + "\n  ".join(shouted[:10]))


# -- tests that pass on nothing ----------------------------------------

def _guards_emptiness(node) -> bool:
    """Does this test establish that something is non-empty?

    STRUCTURAL rather than by string. A first version matched phrases
    like "assert len(" and "> 0", and flagged four tests that were
    properly guarded in ways a phrase list cannot anticipate -- a bare
    `assert voided`, an index `moments[0]` that raises on an empty
    list, `assert control.tests`. Four false alarms on the first run is
    how a check gets switched off.
    """
    import ast

    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            test = sub.test
            # `assert rows`, `assert control.tests` -- truthiness is a
            # non-empty check for a collection.
            if isinstance(test, ast.Name | ast.Attribute):
                return True
            # `assert len(x) ...`, and any comparison at all: `== {..}`,
            # `> 0`, `<= names` all pin a size or a membership.
            if isinstance(test, ast.Compare):
                return True
            # `any(...)` fails on empty, which is the opposite of all().
            if isinstance(test, ast.Call) and getattr(test.func, "id", "") == "any":
                return True
    # `moments[0] == ...` -- indexing an empty list raises, so it is a
    # guard. BUT ONLY AT THE TOP OF AN ASSERTION. A first version
    # counted any integer subscript anywhere in the test, and so counted
    # `row[0]` inside the very comprehension that builds the list under
    # suspicion: put back to their vacuous form, both of the tests this
    # check was written for passed it. A check that cannot catch the
    # thing it was written for is the failure it exists to prevent.
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            for part in _outside_comprehensions(sub.test):
                if isinstance(part, ast.Subscript) and isinstance(part.slice, ast.Constant) \
                        and isinstance(part.slice.value, int):
                    return True
    return False


def _outside_comprehensions(node):
    """Every node under this one, not descending into a comprehension.

    Element access inside `[r[0] for r in rows]` says nothing about
    whether `rows` is empty, so it must not be mistaken for a guard.
    """
    import ast

    comprehensions = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        for child in ast.iter_child_nodes(current):
            if not isinstance(child, comprehensions):
                stack.append(child)


def _unguarded_all_over_queries():
    """all() in an assertion, with nothing establishing the input is non-empty.

    all() over an empty sequence is True, so a test that checks every
    row of a query result passes when the query returns nothing -- which
    is often exactly the failure the test exists to catch.
    """
    import ast

    found = []
    for path in sorted(TESTS.rglob("test_*.py")):
        source = path.read_text()
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test")):
                continue
            uses_all = any(
                isinstance(n, ast.Assert) and isinstance(n.test, ast.Call)
                and getattr(n.test.func, "id", "") == "all"
                for n in ast.walk(node))
            if uses_all and not _guards_emptiness(node):
                found.append(f"{path.name}::{node.name}")
    return found


def test_no_test_asserts_about_every_row_of_nothing():
    # Found by audit: three tests checked every row of something that
    # might have been empty, and passed on empty. One claimed to prove
    # an emoji survived three encodings, asserted a SUBSET, and never
    # checked the emoji at all. All three passed on code that was right,
    # so nothing had ever shown they could not fail.
    unguarded = _unguarded_all_over_queries()
    assert unguarded == [], (
        "these assert something about every item of a collection and "
        "nothing about the collection being non-empty, so they pass on "
        "nothing:\n  " + "\n  ".join(unguarded))
