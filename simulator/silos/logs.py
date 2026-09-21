"""
logs.py  (keeping a statement log readable without losing any of it)

Both SQL silos write every statement they are asked to run, which is
what makes `simulator audit` able to answer "what did that tool
actually do to my database". The record is only worth having if it is
complete, so nothing here throws anything away.

What it does instead is bound the size of any single FILE. A simulated
year produced 14 MB, which is nothing; a world left running produces
whatever it produces, and something that reads a log has to hold a
file at a time. Rotating keeps every line and keeps each file small
enough to work with.

Deciding what may finally be deleted is an operator's call. This
deliberately does not make it.
"""

from pathlib import Path

#: When a log is moved aside. Large enough that an ordinary run never
#: rotates at all, small enough that no single file is unwieldy.
ROTATE_ABOVE_BYTES = 64 * 1024 * 1024


def log_parts(path: Path) -> list[Path]:
    """A log and everything rotated out of it, oldest first.

    Rotation renames a full log to `.1`, then `.2`, so a HIGHER number
    is OLDER -- which is the convention logrotate uses and the opposite
    of what the numbers suggest at a glance.
    """
    rotated = sorted(
        (candidate for candidate in path.parent.glob(f"{path.name}.*")
         if candidate.suffix.lstrip(".").isdigit()),
        key=lambda candidate: int(candidate.suffix.lstrip(".")), reverse=True,
    )
    return [*rotated, *([path] if path.exists() else [])]


def rotate(path: Path, above: int = ROTATE_ABOVE_BYTES) -> Path | None:
    """Move a log aside if it has grown too large, KEEPING it.

    Called before a server opens the file, never while it is being
    written to: a log rotated under an open handle keeps being appended
    to under its old name, so the new file stays empty and the old one
    keeps growing -- which is the failure this exists to prevent.
    """
    if not path.exists() or path.stat().st_size <= above:
        return None
    existing = [int(candidate.suffix.lstrip("."))
                for candidate in path.parent.glob(f"{path.name}.*")
                if candidate.suffix.lstrip(".").isdigit()]
    moved = path.with_suffix(path.suffix + f".{max(existing, default=0) + 1}")
    path.rename(moved)
    return moved


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: this lives under silos/ rather than beside audit.py, which was the
# first attempt. audit.py already reads a silo's log_path, so a silo importing
# audit is upside down -- the file belongs to the silo and the reading of it
# belongs above.
#
# RESOLVED: nothing is deleted. The record exists to answer what a consumer
# actually did, and a rotation that discarded the answer would be worse than a
# large file.
#
# DEFERRED (known, intentional, not yet built): rotation happens only at
# startup, so a single very long run never rotates at all. Rotating a file
# something is appending to needs the server told to reopen it, which is a
# signal on PostgreSQL and a statement on MariaDB, and neither is worth wiring
# until a run is long enough to need it.
