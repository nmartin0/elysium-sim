"""
ports.py  (allocate once, pin forever)

"Random available, but fixed" is two requirements pulling in opposite
directions, and the resolution is timing: choose once, at build, then
never choose again.

WHY NOT A FIXED RANGE STARTING AT 5432. Because the machine running
this almost certainly already has a PostgreSQL on 5432, and a
simulator that fights the developer's own database for a port is a bad
neighbour. Asking the kernel for an unused port avoids the whole
class.

WHY NOT CHOOSE FRESH EVERY RUN. Because a consumer's configuration
names the port. If it moved between runs, every restart of the
simulator would silently invalidate whatever is pointed at it, and the
symptom on the far side is "silo unreachable" -- which reads as a bug
in the consumer rather than as a port that moved. Pinning in a file
beside the world is what makes a configuration written once keep
working.

WHY A CONFLICT IS FATAL RATHER THAN REALLOCATED. Same reason. If a
pinned port is occupied at startup, quietly picking another one
produces exactly the silent invalidation above. Failing with the port
number and the silo name gives someone something to act on.

THE HONEST RACE. Allocation binds to port 0, records what the kernel
assigned, and releases -- so between release and the server binding it
for real, another process could take it. That window is real and
cannot be closed from here without holding the socket and handing the
file descriptor to PostgreSQL, which it has no interface to accept.
What narrows it is that all the sockets are held open TOGETHER until
every port has been chosen, so at least the simulator cannot hand the
same port to two of its own silos -- which is the collision that would
actually happen, since ephemeral ports are reused aggressively.
"""

import json
import socket
from dataclasses import dataclass
from pathlib import Path

#: Sits beside the world's data, not in a config directory: it
#: describes a built world and is meaningless without one.
PORTS_FILENAME = "ports.json"


class PortConflict(Exception):
    """A pinned port is already in use by something else."""


@dataclass(frozen=True)
class PortRegistry:
    """Which port each named database listens on, for one world."""

    path: Path
    ports: dict[str, int]

    def port(self, name: str) -> int:
        if name not in self.ports:
            raise KeyError(
                f"no port assigned to {name!r}; this world has {sorted(self.ports)}"
            )
        return self.ports[name]

    @classmethod
    def allocate(cls, directory: Path, names: list[str]) -> "PortRegistry":
        """Choose a free port per name, once, and write the assignment.

        Every socket is held open until all names have a port, then all
        are closed together. Allocating one at a time and releasing as
        it goes would let the kernel hand the same ephemeral port to
        the second name that it just handed to the first.
        """
        if not names:
            raise ValueError("cannot allocate ports for an empty list of names")
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"duplicate database names: {sorted(duplicates)}")

        held: list[socket.socket] = []
        assigned: dict[str, int] = {}
        try:
            for name in names:
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                probe.bind(("127.0.0.1", 0))
                held.append(probe)
                assigned[name] = probe.getsockname()[1]
        finally:
            for probe in held:
                probe.close()

        registry = cls(path=directory / PORTS_FILENAME, ports=assigned)
        registry.write()
        return registry

    @classmethod
    def load(cls, directory: Path) -> "PortRegistry":
        path = directory / PORTS_FILENAME
        if not path.exists():
            raise FileNotFoundError(
                f"no {PORTS_FILENAME} in {directory} -- this world has not been built"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(path=path, ports={str(k): int(v) for k, v in raw["ports"].items()})

    def write(self) -> None:
        """Persist the assignment, atomically.

        Written to a temporary file and renamed, because a half-written
        ports.json is worse than none: `load()` would raise a JSON
        error naming a parse position rather than saying the world is
        not built, and the actual cause -- a run killed mid-write --
        would be invisible.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"ports": self.ports}, indent=2, sort_keys=True) + "\n"
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)

    def verify_available(self) -> None:
        """Raise unless every pinned port is free to bind.

        Called before starting servers. Reporting the conflict is the
        whole point -- see the module note on why reallocating instead
        would be worse.
        """
        taken = []
        for name, port in sorted(self.ports.items()):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                taken.append(f"{name} (port {port})")
            finally:
                probe.close()
        if taken:
            raise PortConflict(
                "ports pinned for this world are already in use: "
                + ", ".join(taken)
                + f". They are recorded in {self.path}; free them, or rebuild the "
                "world to choose new ones -- but note that anything configured "
                "against the old ports will need updating."
            )


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): allocate() holds every probe socket open until
# all names have been assigned, then closes them together. Closing each before
# opening the next lets the kernel reuse the port it just handed out, so two
# silos in one world can get the same number -- the collision that would
# actually happen, given how aggressively ephemeral ports are recycled.
#
# RESOLVED: a conflict is fatal rather than reallocated. Quietly choosing a
# different port silently invalidates whatever is configured against the old
# one, and the symptom on the far side is "silo unreachable", which reads as a
# bug in the consumer.
#
# UNTESTED, and said plainly: the bind/release/rebind race described in the
# module docstring. Between allocate() releasing a port and PostgreSQL binding
# it, another process can take it. Reproducing that deterministically means
# winning a race on purpose, and a test that hopes for an interleaving is worse
# than no test. It cannot be closed without handing PostgreSQL an open file
# descriptor, which it has no interface to accept. verify_available() narrows
# the window to microseconds by checking immediately before start.
#
# DEFERRED (known, intentional, not yet built): no port RANGE constraint. A
# deployment behind a firewall might need ports from a permitted band, which
# would mean probing candidates rather than asking for zero. Nothing needs it
# yet, and the honest version has to handle exhaustion of the band.
