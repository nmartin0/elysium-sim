import pytest

from simulator.rng import RandomSource, _derive_seed, sample_without_replacement, weighted_choice


def test_derivation_is_stable_across_processes():
    # A literal expected value, deliberately. The whole point of
    # blake2b over Python's hash() is that this number is the same in
    # every process and on every machine; asserting the property
    # loosely ("two calls agree") would pass against hash() too, since
    # a single test process shares one PYTHONHASHSEED. This assertion
    # is the only thing that can catch a regression to a randomized
    # derivation, so the literal is the test.
    assert _derive_seed(1234, "sales") == 7948905441380676770


def test_same_seed_reproduces_a_stream():
    first = RandomSource(99).stream("sales")
    second = RandomSource(99).stream("sales")
    assert [first.random() for _ in range(5)] == [second.random() for _ in range(5)]


def test_streams_are_independent_of_each_other():
    # The property the whole module exists for: drawing from one
    # stream must not shift another. Without named derivation, the
    # extra draws below would move every later "inventory" value.
    undisturbed = RandomSource(7)
    expected = [undisturbed.stream("inventory").random() for _ in range(3)]

    disturbed = RandomSource(7)
    for _ in range(50):
        disturbed.stream("sales").random()
    actual = [disturbed.stream("inventory").random() for _ in range(3)]

    assert actual == expected


def test_stream_is_cached_so_the_sequence_continues():
    # A fresh Random per call would return the same value forever.
    # This is the bug the cache prevents, and it would otherwise be
    # invisible: the data would look plausible and be identical.
    source = RandomSource(3)
    draws = [source.stream("x").random() for _ in range(5)]
    assert len(set(draws)) == 5


def test_different_seeds_diverge():
    assert RandomSource(1).stream("s").random() != RandomSource(2).stream("s").random()


def test_weighted_choice_respects_proportions():
    rng = RandomSource(11).stream("choice")
    counts = {"a": 0, "b": 0}
    for _ in range(4000):
        counts[weighted_choice(rng, {"a": 3.0, "b": 1.0})] += 1
    # 3:1 expected; generous bounds so this cannot flake, tight enough
    # that an implementation ignoring the weights (which would give
    # 1:1) fails decisively.
    assert 2.5 < counts["a"] / counts["b"] < 3.6


def test_weighted_choice_rejects_empty_and_zero_weights():
    rng = RandomSource(1).stream("c")
    with pytest.raises(ValueError, match="at least one option"):
        weighted_choice(rng, {})
    with pytest.raises(ValueError, match="positive number"):
        weighted_choice(rng, {"a": 0.0})


def test_sample_tolerates_a_short_population():
    # random.sample() raises here; every caller in this codebase wants
    # "as many as you have" instead.
    rng = RandomSource(5).stream("s")
    assert len(sample_without_replacement(rng, ["a", "b"], 10)) == 2
    assert sample_without_replacement(rng, [], 3) == []
    assert sample_without_replacement(rng, ["a"], 0) == []
