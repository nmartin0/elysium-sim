"""
worldview.py  (what an event is allowed to ask of the world)

FOURTEEN PLACES SAID `world: Any`, and the reason was an import cycle
rather than indifference: world.py imports simulator.spec, which
imports event.py, so event.py cannot import world.py back. `Any` made
that go away and took mypy with it -- every `world.silo(...)` in the
largest behavioural module in the project was unchecked, which is the
opposite of where you want the checking to be thin.

A PROTOCOL BREAKS THE CYCLE BECAUSE NOTHING HAS TO IMPORT ANYTHING.
World does not inherit from this and does not know it exists; it
satisfies it structurally, by having the members. So the dependency
runs no way at all, and mypy still checks that an emission asking for
`world.databse` is wrong.

WHAT IS IN IT IS WHAT EVENTS ACTUALLY USE -- nine members, counted
from the call sites rather than chosen. That is the point of writing
it down: a Protocol listing everything World can do would be a second
copy of World's interface, which would drift and would tell a reader
nothing. This one says "an event needs a way to reach a silo, to ask
which rows exist, to spawn an entity and to know the time", and a
tenth member appearing here should be a deliberate act.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from simulator.context import EvaluationContext
from simulator.schema import Schema
from simulator.silo import Silo


class Clock(Protocol):
    """The part of the clock an event reads."""

    def now(self) -> datetime: ...


class WritesFiles(Protocol):
    """A silo that publishes a file. See the note in event.py."""

    def write_csv(self, filename: str, header: Sequence[str],
                  rows: Iterable[Sequence[object]], *,
                  atomic: bool = True) -> Path: ...


class ServesCollections(Protocol):
    """A silo that exposes a collection. See the note in event.py."""

    def publish(self, collection: str, records: Sequence[dict]) -> None: ...


@runtime_checkable
class WorldView(Protocol):
    """The world, as an event sees it.

    `runtime_checkable` so a test can assert that a World really does
    satisfy this. Structural typing is checked by mypy and not by the
    interpreter, so without that test the two could drift apart and
    only a type-check nobody ran would notice.

    THE CHECK MUST BE isinstance, NOT issubclass: a Protocol with
    non-method members refuses issubclass entirely, and `clock`, `rng`
    and `transitions` are dataclass fields on World -- present on an
    instance and absent from the class.

    Even then, runtime_checkable verifies only that the attributes
    EXIST. The parameter names are checked by a test of their own,
    because they are part of the contract: `database(silo_name=...)`
    is a legal call and would break if this said `name`.
    """

    @property
    def clock(self) -> Clock: ...

    @property
    def rng(self) -> Any: ...

    @property
    def transitions(self) -> list: ...

    def silo(self, name: str) -> Silo: ...

    def database(self, silo_name: str) -> str: ...

    def schema(self, silo_name: str) -> Schema: ...

    def subject_rows(self, qualified: str) -> list[dict]: ...

    def spawn(self, lifecycle_name: str, entity_id: str) -> Any: ...

    def context(self, stream: str, subject: dict | None = None,
                now: datetime | None = None) -> EvaluationContext: ...


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED: a Protocol rather than moving World somewhere both can import. The
# cycle is world.py -> spec -> event.py, and breaking it by moving World would
# mean moving the thing that owns silos, entities and the clock into a lower
# layer than the things that use it -- which is upside down. Structural typing
# costs nothing and inverts nothing.
#
# RESOLVED: parameter NAMES match World's, not the prettier ones this was first
# written with. A Protocol that renames an argument still satisfies mypy for
# positional calls and breaks every keyword one, which is the kind of difference
# that surfaces in somebody else's code months later.
#
# RESOLVED: the members are counted from the call sites, not copied from World.
# A Protocol mirroring everything World offers would be a second copy of an
# interface, and the two would drift.
#
# RESOLVED: WritesFiles and ServesCollections exist because `world.silo(name)`
# returns the base Silo, and a publication calls write_csv() while an exposure
# calls publish() -- methods only the filedrop and rest silos have. `Any` was
# hiding that. The loader already refuses a publication that does not target a
# filedrop, so the narrowing at the call site is documenting an invariant that
# is checked elsewhere rather than asserting a hope.
#
# DEFERRED (known, intentional, not yet built): `rng`, `spawn` and `context`
# are typed loosely, because tightening them means importing RandomSource,
# Entity and the context's keyword shape -- each fine on its own, and together
# enough surface that it is worth doing when something actually needs it rather
# than now.
