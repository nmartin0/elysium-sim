"""
cleanup.py  (picking up after a simulator that did not get to tidy up)

A world stopped properly leaves nothing running: measured, the ports
are bindable the instant `stop()` returns and the directory is
removable with nothing referencing it. That is not the problem.

THE PROBLEM IS THE SIMULATOR DYING. SIGKILL the process -- or lose the
terminal, or run out of memory -- and the database servers it started
are orphaned. Measured: a PostgreSQL cluster and its six helper
processes carried on serving, holding their port, with nothing left
that knew they existed.

Nothing else finds them. They are not children of anything the shell
will reap, they have no entry in any service manager, and the only
record that they belong to this simulator at all is the path they were
started with.

SO THE PATH IS PART OF WHAT IS MATCHED ON, and the engine binary is
the rest. Killing by recorded pid alone would be wrong in the way
hardest to forgive -- pids are reused and a pid file outlives its
process -- but matching on the path ALONE is very nearly as bad, and
that is not a guess: a first version did exactly that and signalled
the shell running `simulator clean --dir /tmp/crash`, because that
command line mentions /tmp/crash too. So does the grep looking for it,
and so does any editor with a file from the world open.

A process is therefore a candidate only when the program being RUN is
a database server -- the first word of its command line is postgres,
mariadbd or mysqld -- and it names the directory. A shell is not a
database server however much of the path it happens to quote.
"""

import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path

#: How long to wait for a signalled server to go, before saying so.
#: Deliberately generous: a cluster with a large buffer cache takes a
#: moment to write itself out, and killing it harder would corrupt a
#: world somebody may still want to resume.
DEATH_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class Stray:
    """A server still running for a world nothing owns any more."""

    pid: int
    command: str

    @property
    def kind(self) -> str:
        for name in ("postgres", "mariadbd", "mysqld"):
            if name in self.command:
                return name
        return "unknown"


#: Programs this will signal. Anything else mentioning the path is
#: something else's business -- a shell, a grep, an editor -- and a
#: first version that did not check this signalled the very command
#: that invoked it.
ENGINES = ("postgres", "postgresql", "mariadbd", "mysqld", "mariadb-admin")


def strays(directory: Path) -> list[Stray]:
    """Every database server still running for this world directory.

    Two conditions, and both are load-bearing. The command line must
    name the directory, because that is the only record left that the
    server belongs to this world. And the program being run must BE a
    server, because half the processes on a machine mention a path at
    some point and none of the others should be killed for it.

    READ FROM /proc RATHER THAN FROM `ps`, and that is not a
    preference. `ps` truncates each line to the terminal width, and to
    Eighty columns when its output is a pipe -- which is always, here.
    A cluster at /tmp/.../pytest-of-claude/pytest-6/test_probe0/w was
    invisible because the path began at column 62 and the line was cut
    at 80, so `clean` reported nothing to do and left a server running.

    Found only because a test was made to assert its own precondition;
    it had been passing, and so had the control aimed at it.
    """
    resolved = str(Path(directory).resolve())
    literal = str(Path(directory))

    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            # Gone between listing and reading, which is ordinary.
            continue
        command = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if not command:
            # A kernel thread. Never ours, and never killable.
            continue
        if resolved not in command and literal not in command:
            continue
        if not _is_engine(command):
            continue
        found.append(Stray(pid=int(entry.name), command=command))
    return found


def _is_engine(command: str) -> bool:
    """Whether the program being run is a database server.

    The FIRST word, which is the executable. A server started as
    `/usr/lib/postgresql/16/bin/postgres -D ...` qualifies; a shell
    whose arguments happen to contain the word does not, and telling
    those apart is the whole difference between a cleanup tool and a
    hazard.
    """
    first = command.strip().split(maxsplit=1)[0] if command.strip() else ""
    return os.path.basename(first) in ENGINES


def terminate(stray: Stray, *, timeout: float = DEATH_TIMEOUT_SECONDS) -> bool:
    """Ask a stray to stop, and wait to see whether it did.

    SIGTERM, which both engines treat as a request to shut down
    cleanly. Not SIGKILL: a world may be resumed, and a cluster killed
    mid-write is a world that will not open again.
    """
    try:
        os.kill(stray.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        # Someone else's process that happens to mention the path. Not
        # ours to kill, and saying so is more useful than failing.
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(stray.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.1)
    return False


def clean(directory: Path, *, remove: bool = False) -> dict:
    """Stop whatever is still running for this world, and optionally
    delete it.

    Returns what was found and what happened, because a cleanup that
    reports nothing is one nobody can tell apart from a cleanup that
    did nothing.
    """
    directory = Path(directory)
    found = strays(directory)
    stopped: list[Stray] = []
    stubborn: list[Stray] = []
    for stray in found:
        (stopped if terminate(stray) else stubborn).append(stray)

    removed = False
    if remove and not stubborn:
        # Only once nothing is writing to it. Deleting a directory out
        # from under a live server produces a cluster that half exists,
        # which is worse than leaving it.
        shutil.rmtree(directory, ignore_errors=True)
        removed = not directory.exists()

    return {"found": found, "stopped": stopped, "stubborn": stubborn,
            "removed": removed}


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: processes are matched by the world path in their command line AND by
# the program being a database server. Pid files are unreliable because pids are
# reused; the path alone is unreliable because everything mentions paths -- a
# first version signalled the shell that invoked `simulator clean`, the grep
# looking for the cluster, and would have signalled any editor with a file from
# the world open. Both conditions are load-bearing and neither is sufficient.
#
# RESOLVED: SIGTERM and a wait, never SIGKILL. A world may be resumed, and a
# cluster killed mid-write may not open again. A stray that will not go is
# reported rather than forced.
#
# RESOLVED: the world DIRECTORY is not removed by default. It is the record of
# what happened and resume reads it; deleting it as a side effect of tidying up
# processes would throw away a simulated year to reclaim a port.
#
# DEFERRED (known, intentional, not yet built): nothing reclaims a port held by
# a stray this cannot kill -- someone else's process mentioning the same path.
# There is nothing to do about that except say so, which it does.
