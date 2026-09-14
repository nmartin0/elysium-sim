import pytest

from simulator.clock import utc
from simulator.lifecycle import Entity, Lifecycle, Transition, advance, in_state, state_counts
from simulator.rng import RandomSource

CUSTOMER = Lifecycle(
    name="Customer",
    initial="prospect",
    states={
        "prospect": [Transition("active", per_hour=0.5)],
        # 2:1 in favour of staying active at equilibrium.
        "active": [Transition("lapsed", per_hour=0.05)],
        "lapsed": [Transition("active", per_hour=0.10)],
        "churned": [],
    },
)


def _entity(state="prospect", at=None):
    moment = at or utc(2026, 1, 1)
    return Entity(entity_id="e1", lifecycle="Customer", state=state, entered_state_at=moment, created_at=moment)


def _run(tick_seconds, total_hours, seed=17):
    """One population run at a given tick size, returning its census."""
    rng = RandomSource(seed).stream("lifecycle")
    start = utc(2026, 1, 1)
    entities = [
        Entity(entity_id=f"e{i}", lifecycle="Customer", state="prospect", entered_state_at=start, created_at=start)
        for i in range(400)
    ]
    now = start
    for _ in range(int(total_hours * 3600 / tick_seconds)):
        now = now.fromtimestamp(now.timestamp() + tick_seconds, tz=now.tzinfo)
        for entity in entities:
            advance(CUSTOMER, entity, now, tick_seconds, rng)
    return state_counts(entities)


@pytest.mark.parametrize("tick_seconds", [60, 600, 1800])
def test_rates_are_per_hour_not_per_tick(tick_seconds):
    # THE load-bearing invariant of this module: a rate describes the
    # world, a tick describes how finely it is sliced, and changing
    # the slicing must not change the world.
    #
    # This asserts the ANALYTIC value, 1 - exp(-rate * hours), rather
    # than comparing two tick sizes to each other. That distinction is
    # not stylistic -- it is what makes the test able to fail. A first
    # version compared the active:lapsed ratio of a long run at two
    # tick sizes and passed against a deliberately per-tick
    # formulation, because scaling both competing rates identically
    # leaves the equilibrium ratio at 2:1 either way; only the time to
    # reach it changes, and the run was long enough to hide that.
    #
    # Measured, at rate 0.1/hr over one simulated hour: correct gives
    # 9.52% at every tick size, while a per-tick reading gives 99.8%
    # at 60s and 19.0% at 1800s. Those are the numbers this bracket is
    # drawn around.
    solo = Lifecycle(name="Solo", initial="waiting",
                     states={"waiting": [Transition("done", per_hour=0.1)], "done": []})
    rng = RandomSource(23).stream("lifecycle")
    start = utc(2026, 1, 1)
    entities = [
        Entity(entity_id=f"e{i}", lifecycle="Solo", state="waiting", entered_state_at=start, created_at=start)
        for i in range(3000)
    ]
    now = start
    for _ in range(int(3600 / tick_seconds)):
        now = now.fromtimestamp(now.timestamp() + tick_seconds, tz=now.tzinfo)
        for entity in entities:
            advance(solo, entity, now, tick_seconds, rng)

    converted = len(in_state(entities, "done")) / len(entities)
    assert converted == pytest.approx(0.0952, abs=0.015)


def test_equilibrium_matches_the_declared_rates():
    # Separate from the test above, because it checks a different
    # thing: that competing rates settle where their ratio says. The
    # declared 0.10 back and 0.05 away imply 2:1 in favour of active.
    census = _run(tick_seconds=600, total_hours=400)
    assert 1.5 < census["active"] / census["lapsed"] < 2.8


def test_advance_returns_the_state_departed():
    # Returning the previous state, not a bool, is what lets a caller
    # react to a specific move without snapshotting beforehand.
    rng = RandomSource(1).stream("l")
    entity = _entity("prospect")
    now = utc(2026, 1, 1)
    for _ in range(200):
        now = now.fromtimestamp(now.timestamp() + 3600, tz=now.tzinfo)
        departed = advance(CUSTOMER, entity, now, 3600, rng)
        if departed is not None:
            assert departed == "prospect"
            assert entity.state == "active"
            assert entity.entered_state_at == now
            return
    pytest.fail("a 0.5/hour transition never fired in 200 hours")


def test_terminal_states_never_move():
    rng = RandomSource(1).stream("l")
    entity = _entity("churned")
    now = utc(2026, 1, 1)
    for _ in range(500):
        now = now.fromtimestamp(now.timestamp() + 3600, tz=now.tzinfo)
        assert advance(CUSTOMER, entity, now, 3600, rng) is None
    assert entity.state == "churned"


def test_minimum_dwell_blocks_an_instant_transit():
    # Pure exponential timing allows an entity to be created and moved
    # on inside one tick, which is legal under the model and nonsense
    # in the world.
    slow = Lifecycle(
        name="Slow",
        initial="a",
        states={"a": [Transition("b", per_hour=1000.0, min_dwell_seconds=7200)], "b": []},
    )
    rng = RandomSource(2).stream("l")
    entity = Entity(entity_id="x", lifecycle="Slow", state="a",
                    entered_state_at=utc(2026, 1, 1), created_at=utc(2026, 1, 1))
    # An hour in: the rate is enormous but the dwell is not served.
    assert advance(slow, entity, utc(2026, 1, 1, 1), 3600, rng) is None
    assert entity.state == "a"
    # Three hours in: now it is eligible and the rate takes over.
    assert advance(slow, entity, utc(2026, 1, 1, 3), 3600, rng) == "a"
    assert entity.state == "b"


def test_zero_elapsed_time_changes_nothing():
    rng = RandomSource(3).stream("l")
    entity = _entity("active")
    assert advance(CUSTOMER, entity, utc(2026, 1, 1), 0, rng) is None


def test_competing_exits_split_by_their_rates():
    forked = Lifecycle(
        name="Forked",
        initial="start",
        states={
            "start": [Transition("common", per_hour=3.0), Transition("rare", per_hour=1.0)],
            "common": [],
            "rare": [],
        },
    )
    rng = RandomSource(5).stream("l")
    landed = {"common": 0, "rare": 0}
    for index in range(2000):
        entity = Entity(entity_id=f"e{index}", lifecycle="Forked", state="start",
                        entered_state_at=utc(2026, 1, 1), created_at=utc(2026, 1, 1))
        now = utc(2026, 1, 1)
        while entity.state == "start":
            now = now.fromtimestamp(now.timestamp() + 600, tz=now.tzinfo)
            advance(forked, entity, now, 600, rng)
        landed[entity.state] += 1
    assert 2.4 < landed["common"] / landed["rare"] < 3.7


def test_unreachable_target_is_rejected_at_construction():
    # Caught where the declaration is, not hours into a run.
    with pytest.raises(ValueError, match="not declared"):
        Lifecycle(name="Broken", initial="a", states={"a": [Transition("nowhere", per_hour=1.0)]})


def test_initial_state_must_exist():
    with pytest.raises(ValueError, match="initial state"):
        Lifecycle(name="Broken", initial="missing", states={"a": []})


def test_transition_rejects_a_non_positive_rate():
    with pytest.raises(ValueError, match="positive rate"):
        Transition("b", per_hour=0.0)


def test_in_state_and_counts():
    entities = [_entity("active"), _entity("active"), _entity("lapsed")]
    assert len(in_state(entities, "active")) == 2
    assert state_counts(entities) == {"active": 2, "lapsed": 1}
    # Sorted, so two identical runs produce identically-ordered output.
    assert list(state_counts(entities)) == ["active", "lapsed"]


def test_an_entity_keeps_its_identity_and_attributes_across_a_move():
    # Entity's identity and payload are its whole reason to exist as a
    # class rather than a bare state string, so they are worth pinning:
    # advance() must change the state and the clock and touch nothing
    # else. Without this, a transition that rebuilt the entity instead
    # of mutating it would silently drop whatever a pack was carrying.
    rng = RandomSource(21).stream("l")
    start = utc(2026, 1, 1)
    entity = Entity(entity_id="cust_000007", lifecycle="Customer", state="prospect",
                    entered_state_at=start, created_at=start,
                    attributes={"home_store_id": "store_000002", "region": "us-west"})

    now = start
    for _ in range(300):
        now = now.fromtimestamp(now.timestamp() + 3600, tz=now.tzinfo)
        if advance(CUSTOMER, entity, now, 3600, rng) is not None:
            break
    else:
        pytest.fail("a 0.5/hour transition never fired in 300 hours")

    assert entity.entity_id == "cust_000007"
    assert entity.created_at == start
    assert entity.attributes == {"home_store_id": "store_000002", "region": "us-west"}
    assert entity.entered_state_at > start


def test_dwell_is_measured_from_the_last_transition_not_creation():
    # The distinction created_at exists for. An entity created long ago
    # but moved recently has a short dwell, and min_dwell_seconds gates
    # on the dwell.
    created = utc(2026, 1, 1)
    entity = Entity(entity_id="e1", lifecycle="Customer", state="active",
                    entered_state_at=utc(2026, 6, 1), created_at=created)
    assert entity.dwell_seconds(utc(2026, 6, 2)) == 86400
    assert entity.created_at == created
