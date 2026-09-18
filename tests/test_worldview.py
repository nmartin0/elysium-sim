"""The Protocol that lets mypy see into events.

Fourteen places said `world: Any`, and the reason was an import cycle
rather than indifference: world.py imports simulator.spec, which
imports event.py. `Any` made that go away and took the type checking
with it, in the largest behavioural module in the project.
"""

import inspect
from datetime import UTC, datetime

from simulator.clock import SimulatedClock
from simulator.ports import PortRegistry
from simulator.rng import RandomSource
from simulator.world import World
from simulator.worldview import ServesCollections, WorldView, WritesFiles


def test_a_world_really_satisfies_the_view(tmp_path):
    # isinstance, NOT issubclass: a Protocol with non-method members
    # refuses issubclass entirely, and `clock`, `rng` and `transitions`
    # are dataclass fields -- present on an instance and absent from
    # the class. A first version of this test asserted issubclass and
    # failed with a message about protocols rather than about the
    # world.
    from simulator.spec import load_spec

    world = World(
        pack=load_spec({"pack": "p",
                        "silos": {"ops": {"kind": "filedrop"}},
                        "schemas": {}}),
        schemas={}, silos={}, ports=PortRegistry(tmp_path, {}),
        rng=RandomSource(1),
        clock=SimulatedClock(start=datetime(2026, 1, 1, tzinfo=UTC)),
    )
    assert isinstance(world, WorldView)


def test_the_view_asks_for_what_events_use_and_no_more():
    # Counted from the call sites rather than copied from World. A
    # Protocol mirroring everything World offers would be a second copy
    # of an interface, and the two would drift.
    expected = {"clock", "rng", "transitions", "silo", "database", "schema",
                "subject_rows", "spawn", "context"}
    declared = {name for name in dir(WorldView) if not name.startswith("_")}
    assert declared == expected


def test_every_member_the_view_names_exists_on_the_world():
    # Fields are on the instance, methods on the class, so both are
    # checked -- the first version looked only at the class and
    # declared `clock` missing.
    annotations = set(World.__annotations__)
    for name in (n for n in dir(WorldView) if not n.startswith("_")):
        assert hasattr(World, name) or name in annotations, name


def test_the_call_signatures_match():
    # runtime_checkable does NOT check signatures, so a member whose
    # arguments changed would satisfy issubclass and fail at the call.
    # `context` is the one this caught: the view first declared
    # `**kwargs` and the world takes (stream, subject, now).
    for name in ("silo", "database", "schema", "subject_rows", "spawn", "context"):
        view = inspect.signature(getattr(WorldView, name))
        real = inspect.signature(getattr(World, name))
        assert list(view.parameters) == list(real.parameters), (
            f"{name}: view takes {list(view.parameters)}, "
            f"world takes {list(real.parameters)}")


def test_the_silo_protocols_match_the_silos_that_implement_them():
    # `world.silo(name)` returns the base Silo, and a publication calls
    # write_csv() while an exposure calls publish() -- methods only the
    # filedrop and rest silos have. `Any` was hiding that.
    from simulator.silos.filedrop import FileDropSilo
    from simulator.silos.rest import RestSilo

    assert hasattr(FileDropSilo, "write_csv")
    assert hasattr(RestSilo, "publish")
    for protocol, implementation, method in ((WritesFiles, FileDropSilo, "write_csv"),
                                             (ServesCollections, RestSilo, "publish")):
        declared = inspect.signature(getattr(protocol, method))
        real = inspect.signature(getattr(implementation, method))
        assert list(declared.parameters) == list(real.parameters), method
