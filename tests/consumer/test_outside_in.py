"""The rule that makes this whole directory mean something.

These tests are supposed to be a standalone client. If one of them
reaches into `simulator` -- for a constant, a helper, anything -- it
stops testing what a real consumer would meet and starts testing the
simulator against itself, which the other six hundred tests already do.

Checked mechanically, because it is exactly the kind of rule that
erodes one convenient import at a time.
"""

import ast
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
#: This module is the exception: it has to read the others to check
#: them, but it still imports nothing from the package.
MODULES = sorted(path for path in HERE.glob("*.py"))


def imported_names(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("path", MODULES, ids=lambda path: path.name)
def test_nothing_here_imports_the_simulator(path):
    offending = sorted(name for name in imported_names(path)
                       if name == "simulator" or name.startswith("simulator."))
    assert offending == [], (
        f"{path.name} imports {offending}. These tests are a standalone "
        f"client; reaching into the package makes them test the simulator "
        f"against itself, which the rest of the suite already does."
    )


def test_there_is_something_here_to_check():
    # A directory that has quietly emptied would pass every assertion
    # above.
    assert len(MODULES) >= 4
    assert any(path.name == "test_reading.py" for path in MODULES)
