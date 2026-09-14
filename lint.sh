#!/bin/sh
# lint.sh -- the four code-quality tools this project uses, and why each
# earns its place. Carried over from the Elysium project this was split
# out of; the reasoning is restated rather than referenced because this
# repository has to stand on its own.
#
#   Ruff    -- is this code well-formed, locally? (unused imports,
#              undefined names, real Python gotchas, import order)
#   MyPy    -- do the types flowing through this actually agree? (the
#              only one of the four that understands data SHAPE)
#   Vulture -- does anything still use this, at all? (whole-program;
#              Ruff structurally cannot answer this, being per-file)
#   Import Linter -- is X allowed to import Y AT ALL? (whole-program,
#              and a different question again: none of the other three
#              understand architectural DIRECTION)
#
# Runs all four regardless of earlier failures, so one run shows every
# issue rather than just the first tool's, but exits non-zero if any
# found something.
#
#   ./lint.sh

set -u
STATUS=0

echo "--- ruff check ---"
ruff check || STATUS=1

echo
echo "--- mypy ---"
mypy || STATUS=1

echo
echo "--- vulture ---"
vulture || STATUS=1

echo
echo "--- lock files ---"
# A lock that has drifted from its requirements reads as a guarantee and
# is not one. Cheap and offline: it catches the failure that actually
# happens, a dependency added or removed without regenerating.
python3 -m scripts.check_lockfiles || STATUS=1

echo
echo "--- import-linter ---"
lint-imports || STATUS=1

echo
if [ "$STATUS" -eq 0 ]; then
    echo "All checks passed."
else
    echo "One or more checks failed -- see above."
fi
exit "$STATUS"
