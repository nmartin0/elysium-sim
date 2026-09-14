"""
check_lockfiles.py  (a lock that has drifted is a guarantee that is not one)

WHAT THIS CHECKS, AND WHAT IT DELIBERATELY DOES NOT. It confirms that
every package named in a requirements file appears in the lock
generated from it, and that the lock's own header records the command
it came from. It does NOT re-resolve the dependency graph -- that
needs the network, takes seconds, and would make `./lint.sh` fail on a
plane.

That narrower check is the one that catches the failure which actually
happens: someone adds a dependency to requirements.txt, uses it, and
forgets to regenerate. The version-skew case (a lock regenerated
against a different index) is real but rare, and catching it is what
CI with a network is for.

Exits non-zero with a message naming the file and the fix.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: (requirements files that feed it, the lock they produce). The first
#: entry's lock is a subset of the second's by construction, which is
#: why the dev lock is compiled from both files rather than from the
#: dev one alone.
PAIRS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("requirements.txt",), "requirements.lock"),
    (("requirements.txt", "requirements-dev.txt"), "requirements-dev.lock"),
)

#: A requirement line: name, then any of the version specifier
#: characters or an extras bracket. `psycopg[binary]>=3.2,<4` -> psycopg
_REQUIREMENT = re.compile(r"^([A-Za-z0-9._-]+)")


def _declared(path: Path) -> set[str]:
    names = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _REQUIREMENT.match(line)
        if match:
            # Normalized the way PyPI normalizes: case-insensitive, and
            # underscore, dot and hyphen are all equivalent. Without
            # this, `import-linter` in a requirements file and
            # `import_linter` in a lock read as different packages.
            names.add(_normalize(match.group(1)))
    return names


def _locked(path: Path) -> set[str]:
    names = set()
    for line in path.read_text().splitlines():
        if line.startswith((" ", "\t", "#")) or not line.strip():
            continue
        match = _REQUIREMENT.match(line.strip())
        if match:
            names.add(_normalize(match.group(1)))
    return names


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def main() -> int:
    problems: list[str] = []
    for sources, lock_name in PAIRS:
        lock_path = ROOT / lock_name
        if not lock_path.exists():
            problems.append(f"{lock_name} is missing entirely")
            continue

        declared: set[str] = set()
        for source in sources:
            declared |= _declared(ROOT / source)
        missing = sorted(declared - _locked(lock_path))
        if missing:
            problems.append(
                f"{lock_name} does not contain {missing}, declared in "
                f"{' + '.join(sources)}"
            )

        header = lock_path.read_text(encoding="utf-8").split("\n", 3)[:3]
        if not any("uv pip compile" in line for line in header):
            problems.append(
                f"{lock_name} has no generation header -- it may have been "
                f"hand-edited, which makes its hashes unverifiable"
            )

    if problems:
        print("Lock files have drifted from their requirements:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("\nRegenerate with:", file=sys.stderr)
        print("  uv pip compile requirements.txt --generate-hashes "
              "-o requirements.lock", file=sys.stderr)
        print("  uv pip compile requirements.txt requirements-dev.txt "
              "--generate-hashes -o requirements-dev.lock", file=sys.stderr)
        return 1

    print("Lock files agree with their requirements.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
