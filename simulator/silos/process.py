"""
process.py  (watching a server process that is not a child of ours)

Both SQL silos start a server that outlives the call that started it,
and then have to answer two questions about a process they do not own:
is it still there, and has it finished dying. Both had identical
answers -- `_recorded_pid` was 99% the same between them and
`_await_death` was 100%, comment included.

The PID comes FROM A file, not from a Popen handle, and that is the
whole reason this is awkward. `pg_ctl` daemonises, so the process that
ends up running the server is not the one we spawned; the only durable
handle is the pid the server writes down. Which means every question
about it goes through the filesystem and can be answered by a stale
file left by something that died badly.
"""

import os
import time
from pathlib import Path


def recorded_pid(path: Path) -> int | None:
    """The pid a server wrote down, if it wrote one we can read.

    Every failure is the same answer -- no pid -- because they mean
    the same thing to a caller: there is nothing here to talk to. A
    missing file, an empty one, and one holding rubbish after a bad
    shutdown are all "not running" as far as anything upstream cares.
    """
    if not path.exists():
        return None
    try:
        return int(path.read_text().split()[0])
    except (ValueError, IndexError, OSError):
        return None


def is_alive(pid: int | None) -> bool:
    """Whether a process exists, without disturbing it.

    Signal 0 is the documented way to ask: it performs the permission
    checks and finds the process, and delivers nothing.

    PermissionError means it exists. That is the whole subtlety, and
    catching bare OSError got it backwards: kill(pid, 0) raises EPERM
    precisely when the process is there and is not ours. Measured --
    is_alive(1) said False for PID 1.

    It mattered because await_death is the only caller: a process that
    could not be signalled read as already dead, so terminate() would
    return claiming a silo was down while it was up. That is exactly
    the bug await_death was written to prevent, arrived at from the
    other direction.
    """
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # There, and someone else's. "Running" is the only safe
        # reading: something holds that pid, and a caller must not
        # assume its port is free.
        return True
    except OSError:
        return False
    return True


def await_death(pid: int, timeout: float = 10.0) -> None:
    """Wait for a killed process to actually be gone.

    Signalling is asynchronous, so a terminate() that returns as soon
    as it has signalled is not true when it says so -- and a caller
    asking is_reachable() straight afterwards gets a racy answer.
    Found by a status table reporting a silo it had just killed as up.

    Returns on timeout rather than raising: the caller asked for the
    process to go away, and a process that will not is a condition for
    whatever checks reachability next, not an exception from the
    killing.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return
        time.sleep(0.02)


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: is_alive distinguishes ProcessLookupError from PermissionError. It
# caught bare OSError when this module was extracted, which read a process that
# could not be signalled as dead -- while PostgresSilo.is_reachable, which the
# extraction was supposed to replace, had always got it right and was left
# behind as a divergent copy. Two liveness checks disagreeing about the same
# case is worse than either alone.
#
# RESOLVED: recorded_pid returns None for every kind of failure rather than
# distinguishing them. A missing file, an empty one and a corrupt one all mean
# "nothing to talk to" to every caller there is, and three answers where one
# will do is three things to handle at each call site.
#
# DEFERRED (known, intentional, not yet built): nothing checks that the pid in
# the file is still the process that wrote it. A pid is reused after enough
# churn, so a stale file could name something else entirely -- and is_alive
# would say yes about a stranger. Checking properly means comparing start times
# through /proc, which is Linux-only; the current risk is a long-lived world
# whose server died and whose pid was recycled, which has not been seen.
