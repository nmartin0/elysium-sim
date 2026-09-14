import json
import socket

import pytest

from simulator.sql.ports import PORTS_FILENAME, PortConflict, PortRegistry


def test_allocation_gives_every_name_a_distinct_port(tmp_path):
    registry = PortRegistry.allocate(tmp_path, ["pos", "books", "crm"])
    assert sorted(registry.ports) == ["books", "crm", "pos"]
    assert len(set(registry.ports.values())) == 3


def test_distinctness_holds_at_a_scale_that_can_actually_collide(tmp_path):
    # THE test for holding every probe socket open until all ports are
    # chosen, and it only means anything at scale. Measured on Linux:
    # releasing each probe before opening the next produces 294-299
    # distinct ports out of 300 across five trials, never 300, because
    # the kernel cycles through the ephemeral range and comes back
    # round. At three names it collides essentially never, so the test
    # above passes against the broken implementation and proves nothing.
    #
    # Holding them together cannot collide -- two sockets cannot bind
    # the same port without SO_REUSEPORT -- so this is deterministic on
    # correct code, not a probabilistic assertion.
    names = [f"silo_{index:03d}" for index in range(300)]
    registry = PortRegistry.allocate(tmp_path, names)
    assert len(set(registry.ports.values())) == 300


def test_allocated_ports_are_really_free(tmp_path):
    registry = PortRegistry.allocate(tmp_path, ["pos"])
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", registry.port("pos")))
    finally:
        probe.close()


def test_the_assignment_survives_a_reload(tmp_path):
    # The whole point of pinning: a configuration written against these
    # ports keeps working across runs.
    original = PortRegistry.allocate(tmp_path, ["pos", "books"])
    reloaded = PortRegistry.load(tmp_path)
    assert reloaded.ports == original.ports


def test_the_file_is_readable_by_a_human(tmp_path):
    PortRegistry.allocate(tmp_path, ["pos"])
    payload = json.loads((tmp_path / PORTS_FILENAME).read_text())
    assert isinstance(payload["ports"]["pos"], int)


def test_loading_an_unbuilt_world_says_so(tmp_path):
    # Rather than a JSON parse error naming a byte offset.
    with pytest.raises(FileNotFoundError, match="has not been built"):
        PortRegistry.load(tmp_path)


def test_an_unknown_name_lists_what_exists(tmp_path):
    registry = PortRegistry.allocate(tmp_path, ["pos"])
    with pytest.raises(KeyError, match="pos"):
        registry.port("books")


def test_allocation_rejects_empty_and_duplicate_names(tmp_path):
    with pytest.raises(ValueError, match="empty list"):
        PortRegistry.allocate(tmp_path, [])
    with pytest.raises(ValueError, match="duplicate"):
        PortRegistry.allocate(tmp_path, ["pos", "pos"])


def test_verify_available_passes_when_the_ports_are_free(tmp_path):
    PortRegistry.allocate(tmp_path, ["pos", "books"]).verify_available()


def test_verify_available_names_the_conflict_rather_than_moving(tmp_path):
    # A pinned port being taken is fatal on purpose. Quietly choosing
    # another silently invalidates whatever is configured against the
    # old one, and the symptom on the far side is "silo unreachable",
    # which reads as a bug in the consumer.
    registry = PortRegistry.allocate(tmp_path, ["pos", "books"])
    squatter = socket.socket()
    squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    squatter.bind(("127.0.0.1", registry.port("books")))
    squatter.listen(1)
    try:
        with pytest.raises(PortConflict) as raised:
            registry.verify_available()
        message = str(raised.value)
        assert "books" in message
        assert str(registry.port("books")) in message
        assert PORTS_FILENAME in message
    finally:
        squatter.close()
    # And the assignment is unchanged -- it did not reallocate.
    assert PortRegistry.load(tmp_path).ports == registry.ports


def test_writing_is_atomic_and_leaves_no_temporary(tmp_path):
    # A half-written ports.json is worse than none: load() would raise a
    # parse error naming a byte offset, and the real cause -- a run
    # killed mid-write -- would be invisible.
    PortRegistry.allocate(tmp_path, ["pos"])
    assert list(tmp_path.glob("*.tmp")) == []
    assert (tmp_path / PORTS_FILENAME).exists()


def test_allocation_creates_the_directory_if_needed(tmp_path):
    nested = tmp_path / "var" / "worlds" / "retail"
    registry = PortRegistry.allocate(nested, ["pos"])
    assert registry.path.exists()
