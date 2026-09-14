"""
rng.py  (seeded randomness, split into independent named streams)

ONE SEED PER RUN, BUT NOT ONE GENERATOR PER RUN. That distinction is
the whole reason this file exists rather than callers sharing a single
random.Random.

With one shared generator, every draw depends on every draw before it.
Add one extra product name to a pack's seed step and every sale, every
delay, every lifecycle transition for the rest of the run shifts --
because they are all pulling from the same sequence. A run that was
reproducible stops being comparable to the run before it, which
destroys the main use of seeding: changing one thing and seeing what
that one thing did.

Named streams fix it. `stream("sales")` and `stream("inventory")` are
derived from the run seed and their own name, so they are reproducible
together and independent of each other. Adding a draw to one leaves
the other byte-identical. This is the same reasoning behind numpy's
SeedSequence spawning and JAX's split keys; the mechanism here is
deliberately the simplest thing that gets the property -- hashing the
name with the seed -- because the simulator needs independence, not
cryptographic stream quality.

NOT `random` MODULE-LEVEL FUNCTIONS, anywhere, ever. Those share one
global generator that any imported library can also draw from, which
would make reproducibility depend on what else happened to be
imported.
"""

import hashlib
import random
from collections.abc import Sequence


def _derive_seed(run_seed: int, name: str) -> int:
    """A stable child seed from a run seed and a stream name.

    blake2b rather than Python's hash(): hash() is randomized per
    process by PYTHONHASHSEED, so deriving from it would make runs
    reproducible only within a single interpreter -- the failure mode
    this whole module exists to avoid, and one that would look fine in
    a test suite that runs in one process.
    """
    digest = hashlib.blake2b(f"{run_seed}:{name}".encode(), digest_size=8)
    return int.from_bytes(digest.digest(), "big")


class RandomSource:
    """A run's randomness, divided into independent named streams."""

    def __init__(self, run_seed: int) -> None:
        self.run_seed = run_seed
        self._streams: dict[str, random.Random] = {}

    def stream(self, name: str) -> random.Random:
        """The generator for one named stream, created on first use.

        Cached, so repeated calls continue one sequence rather than
        restarting it -- a fresh generator per call would return the
        same value every time, which is the bug this cache exists to
        prevent and which a test below deliberately covers.
        """
        if name not in self._streams:
            self._streams[name] = random.Random(_derive_seed(self.run_seed, name))
        return self._streams[name]


def weighted_choice[T](rng: random.Random, weights: dict[T, float]) -> T:
    """One key, chosen in proportion to its weight.

    random.choices() already does this, but takes parallel lists and
    returns a list. Lifecycle transitions are naturally written as
    {state: weight}, and unpacking that into two lists at every call
    site -- while keeping their order in step -- is exactly the kind
    of duplication that goes wrong silently.
    """
    if not weights:
        raise ValueError("weighted_choice needs at least one option")
    total = sum(weights.values())
    if total <= 0:
        raise ValueError(f"weights must sum to a positive number, got {total}")
    threshold = rng.random() * total
    running = 0.0
    for key, weight in weights.items():
        running += weight
        if running >= threshold:
            return key
    # Reachable only through floating-point accumulation landing a
    # hair short of the total. Returning the last key is correct
    # rather than a fallback: it is the bucket the threshold fell in.
    return list(weights)[-1]


def sample_without_replacement[T](rng: random.Random, population: Sequence[T], count: int) -> list[T]:
    """Up to `count` distinct items, tolerating a short population.

    random.sample() raises when asked for more than exists. Every
    caller here wants "as many as you have" instead -- a shop with
    three products in stock can still make a sale -- and writing
    min(count, len(...)) at each call site is a detail that gets
    forgotten at exactly one of them.
    """
    if count <= 0 or not population:
        return []
    return rng.sample(list(population), min(count, len(population)))


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): an earlier version derived stream seeds with
# Python's built-in hash(). That is randomized per process unless PYTHONHASHSEED
# is pinned, so two runs of the same seed in different processes diverged while
# every test -- all in one process -- passed. blake2b is stable across
# processes and machines; tests/simulator/test_rng.py asserts a literal
# expected value so a future change to the derivation cannot pass silently.
#
# DEFERRED (known, intentional, not yet built): no stream hierarchy (a stream
# spawning sub-streams). Flat names have been enough for one pack. The moment
# a pack wants per-entity independent streams -- "this store's sales are
# reproducible regardless of what other stores did" -- the natural extension is
# stream("sales", store_id), and _derive_seed already takes a string that could
# carry it. Not built until a pack actually needs it (Principle 7).
