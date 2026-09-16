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
