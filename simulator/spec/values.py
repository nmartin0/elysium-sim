"""
values.py  (reading a scalar out of a pack file, and saying where)

The first piece taken out of a 1,500-line loader, and it is first
because every other section depends on it: silos, schemas, lifecycles,
seed steps, events and migrations all need to pull a string or a
mapping out of YAML and complain usefully when it is not there.

WHAT THEY HAVE IN COMMON is not the reading -- that is one line -- but
the COMPLAINT. A pack author looking at "expected a string" learns
nothing; looking at "events.sale.emits[0].table: expected a string, got
a list" learns exactly what to change. Every function here takes the
path it is reading, and that is the whole reason they exist as
functions rather than as dictionary lookups.

YAML 1.1 is why _check_keys_are_strings exists at all: it reads bare
`on`, `off`, `yes`, `no` and `null` as booleans and None, so a pack
declaring a column called `on` gets a dictionary keyed by True. The
error names the key, because the file looks perfectly reasonable.
"""

from typing import Any

#: Suffixes accepted in a duration like `4h` or `30m`. Durations appear
#: as min_dwell on lifecycle transitions and read far better than a
#: count of seconds -- `min_dwell: 7d` against `min_dwell: 604800`.
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class PackError(Exception):
    """Something in a pack file is wrong, and where."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


def _require_mapping(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        raise PackError(path, f"must be a mapping, got {type(value).__name__}")
    _check_keys_are_strings(value, path)
    return value

def _check_keys_are_strings(mapping: dict, path: str) -> None:
    """Catch YAML's implicit typing before it confuses someone.

    A key of None or True did not come from an author writing None or
    True. It came from them writing `null:`, `on:`, `off:`, `yes:` or
    `no:`, which YAML 1.1 -- and therefore PyYAML -- reads as those
    values rather than as strings. The complaint otherwise names a key
    that does not appear anywhere in their file.
    """
    offenders = [key for key in mapping if not isinstance(key, str)]
    if offenders:
        raise PackError(
            path,
            f"has non-string key(s) {offenders}. YAML reads the bare words null, "
            f"on, off, yes and no as values rather than strings, so `null: false` "
            f"becomes a None key. Quote the word, or use the intended spelling "
            f"(nullability is `nullable`)."
        )

def _mapping(raw: dict, key: str, path: str) -> dict:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise PackError(path, f"must be a mapping, got {type(value).__name__}")
    return value

def _string(raw: dict, key: str, path: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise PackError(f"{path}.{key}" if key not in path else path,
                        f"must be a non-empty string, got {value!r}")
    return value

def _duration(value: Any, path: str) -> float:
    """Seconds from `4h`, `30m`, `7d`, or a bare number."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or len(value) < 2:
        raise PackError(path, f"{value!r} is not a duration like '4h' or '30m'")
    # The number is checked first, deliberately. "soon" ends in "n",
    # and reporting an unknown unit 'n' sends someone looking for a
    # typo in a unit they never wrote. Something that is not a number
    # followed by a unit is not a duration at all, and should say so.
    try:
        amount = float(value[:-1])
    except ValueError as error:
        raise PackError(path, f"{value!r} is not a duration like '4h' or '30m'") from error
    unit = value[-1].lower()
    if unit not in _DURATION_UNITS:
        raise PackError(
            path, f"{value!r} has unknown unit {unit!r}; use one of {sorted(_DURATION_UNITS)}"
        )
    return amount * _DURATION_UNITS[unit]


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: PackError moved here with them, because every one of these raises it
# and a helper module that imported its own exception back from the module it
# was extracted from would be a cycle waiting to happen.
#
# DEFERRED (known, intentional, not yet built): the loader still holds silos,
# schemas, lifecycles, seed, events and migrations. The sections are marked with
# comments and come out one at a time, each verified -- treating the split as
# all-or-nothing is what left it undone for so long.
