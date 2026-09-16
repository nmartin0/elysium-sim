"""
lifecycle.py  (entities that hold a state and move between states)

An entity here is a thing with an identity that persists and a state
that changes: a customer, a work order, an aircraft, a permit
application. Rows in an append-only ledger are not entities -- a sale
happens once and is never revised -- and keeping that distinction
sharp is what stops the engine from carrying mutable state for things
that do not have any.

Transition rates are per hour, not per tick. This is the one design
decision in this file that everything else follows from, and getting
it wrong is subtle. A per-tick probability means the pack's behaviour
changes when the tick size changes: run the same world at 30-second
ticks instead of 5-minute ticks and customers churn ten times faster,
with nothing in the declaration to suggest it would. Rates per hour
are a property of the world; ticks are a property of how finely the
run happens to be sliced. The conversion below makes a run's
behaviour identical regardless.

Competing risks, not a sequence of independent coin flips. A state
with three exits is not three separate chances to leave; it is one
exponential race between three hazards, where the total rate decides
whether the entity moves and the relative rates decide WHERE it goes.
Flipping each exit separately over-counts: with three exits at 0.1/hr
each, independent flips leave a real chance of two firing in one tick
and the code silently taking whichever it checked first. This is
standard discrete-event survival modelling and it is also simply less
code.

Minimum dwell exists because pure exponential timing produces
instantaneous transits -- an entity created, activated and churned
inside one tick -- which are legal under the model and nonsense in the
world. A work order does not go from dispatched to invoiced in nine
seconds.
"""

import math
import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from simulator.rng import weighted_choice


@dataclass(frozen=True)
class Transition:
    """One way out of a state.

    Frozen: a transition is a declaration about the world, read many
    times and never edited. Mutating one at runtime would change a
    pack's behaviour partway through a run with nothing recording it.
    """

    to_state: str
    #: Expected occurrences per hour, for an entity sitting in the
    #: source state. 1.0 means "typically about an hour"; 0.01 means
    #: "typically about four days".
    per_hour: float
    #: Simulated seconds an entity must have held the state before
    #: this exit is available at all.
    min_dwell_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.per_hour <= 0:
            raise ValueError(f"transition to {self.to_state!r} needs a positive rate, got {self.per_hour}")
        if self.min_dwell_seconds < 0:
            raise ValueError(f"transition to {self.to_state!r} has a negative dwell")


@dataclass(frozen=True)
class Lifecycle:
    """A named state machine, with its states and their exits.

    A state absent from `states`, or present with an empty list, is
    terminal: nothing leaves it, and advance() returns None forever. That is a real modelling need --
    churned, closed, scrapped -- and representing it by omission
    rather than a flag means a pack cannot declare a state both
    terminal and exit-bearing.
    """

    name: str
    initial: str
    states: dict[str, list[Transition]]

    def __post_init__(self) -> None:
        if self.initial not in self.states:
            raise ValueError(f"{self.name}: initial state {self.initial!r} is not among its states")
        for state, transitions in self.states.items():
            for transition in transitions:
                if transition.to_state not in self.states:
                    # Caught at construction rather than when an
                    # entity first attempts the move, which could be
                    # hours into a run and far from the declaration.
                    raise ValueError(
                        f"{self.name}: state {state!r} can reach {transition.to_state!r}, "
                        f"which is not declared"
                    )


@dataclass
class Entity:
    """One simulated thing, with identity, state and attributes.

    Mutable, unlike Lifecycle and Transition: this is the part that
    genuinely changes. `attributes` carries whatever the pack needs to
    remember between ticks that is not the state itself -- a running
    balance, a home store, the product mix this customer favours.
    """

    entity_id: str
    lifecycle: str
    state: str
    entered_state_at: datetime
    created_at: datetime
    attributes: dict = field(default_factory=dict)

    def dwell_seconds(self, now: datetime) -> float:
        return (now - self.entered_state_at).total_seconds()


def available_transitions(lifecycle: Lifecycle, entity: Entity, now: datetime) -> list[Transition]:
    """The exits an entity has actually earned, dwell included."""
    dwell = entity.dwell_seconds(now)
    return [t for t in lifecycle.states.get(entity.state, []) if dwell >= t.min_dwell_seconds]


def advance(lifecycle: Lifecycle, entity: Entity, now: datetime,
            elapsed_seconds: float, rng: random.Random) -> str | None:
    """Move the entity if its race fires. Returns the state left, or None.

    Returning the previous state rather than a bool is what lets a
    caller react to the specific move -- "a work order left
    `dispatched`" is actionable, "something changed" is not -- without
    the caller having to snapshot the state beforehand and compare.

    The entity is mutated in place, deliberately: it is the same
    object the pack holds in its registry, and returning a copy would
    mean every caller remembering to store it back.
    """
    if elapsed_seconds <= 0:
        return None
    exits = available_transitions(lifecycle, entity, now)
    if not exits:
        return None

    hours = elapsed_seconds / 3600.0
    total_rate = sum(t.per_hour for t in exits)
    # Probability that any exit fires in this interval, from the
    # exponential survival function. Bounded above by 1 by
    # construction, so no clamping is needed even for a long tick.
    if rng.random() >= 1.0 - math.exp(-total_rate * hours):
        return None

    # It fired; which one is proportional to the competing rates.
    chosen = weighted_choice(rng, {t: t.per_hour for t in exits})
    previous = entity.state
    entity.state = chosen.to_state
    entity.entered_state_at = now
    return previous


def in_state(entities: Iterable[Entity], state: str) -> list[Entity]:
    """Every entity currently in one state.

    A one-line comprehension, extracted because packs ask this at
    every emission point ("which customers are active enough to buy")
    and the alternative is the same filter written in five places,
    where one of them eventually gets the state name wrong.
    """
    return [entity for entity in entities if entity.state == state]


def state_counts(entities: Iterable[Entity]) -> dict[str, int]:
    """A census by state, for status output and for tests.

    Sorted by state name so the output is stable between runs --
    dict insertion order here would otherwise depend on which entity
    happened to transition first, making two identical runs produce
    differently-ordered status text.
    """
    counts: dict[str, int] = {}
    for entity in entities:
        counts[entity.state] = counts.get(entity.state, 0) + 1
    return dict(sorted(counts.items()))


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): transition rates are per hour, not per tick.
# An earlier sketch used per-tick probabilities, which made the same pack
# behave differently at different tick sizes with nothing in the declaration
# hinting at it. tests/simulator/test_lifecycle.py asserts the invariant
# directly: the same world run at two tick sizes reaches the same equilibrium.
# That test fails against a per-tick formulation, which is the negative control.
#
# RESOLVED: competing risks rather than one flip per exit. Independent flips
# over-count when several exits are available -- two can fire in one tick and
# the code silently takes whichever it evaluated first, which also makes the
# outcome depend on declaration order.
#
# DEFERRED (known, intentional, not yet built): no time-varying transition
# rates. Every rate is constant per state. Real lifecycles are not always --
# an invoice's chance of being paid spikes near its due date rather than
# staying flat -- and the natural extension is a rate that takes the entity
# and returns a float. Not built until a pack needs it, because the constant
# form covers everything the retail pack does and a callable rate makes the
# declaration much harder to read.
#
# DEFERRED: nothing here persists. Entities live in memory for the run's
# duration. Restart-resumable runs need a store, and the natural home is
# alongside the migration log in database.py so that world state and schema
# state are recovered together or not at all. Not built yet: the packs seed
# and backfill quickly enough that resuming has not been worth the coupling.
