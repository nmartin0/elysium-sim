"""Tests for the generator hierarchy.

Three things are worth pinning here, in rough order of importance:
that a malformed declaration fails when the pack is READ rather than
mid-run, that templates cannot be used to walk the object graph, and
that values come out the type the schema layer expects.
"""

from datetime import UTC, datetime
from decimal import Decimal
from random import Random

import pytest

from simulator.context import EvaluationContext, ReferenceError_
from simulator.generators import GENERATORS, GeneratorError, build

NOW = datetime(2026, 3, 2, 10, 15, tzinfo=UTC)


def make_context(seed: int = 1, **kwargs) -> EvaluationContext:
    return EvaluationContext(now=NOW, rng=Random(seed), **kwargs)


def generate(spec: dict, context: EvaluationContext | None = None):
    return build(spec).value(context or make_context())


# -- the registry ----------------------------------------------------

def test_every_registered_generator_answers_to_its_own_name():
    for name, generator in GENERATORS.items():
        assert generator.name == name


def test_the_set_of_generators_is_what_it_claims():
    assert set(GENERATORS) == {
        "constant", "id", "occurrence_id", "now", "choice", "weighted",
        "integer", "decimal", "reference", "expression", "template", "prose",
        "choose",
    }


def test_an_unknown_generator_lists_what_exists():
    with pytest.raises(GeneratorError, match="no generator called 'wibble'"):
        build({"generator": "wibble"})


def test_a_declaration_must_say_which_generator():
    with pytest.raises(GeneratorError, match="does not say which generator"):
        build({"prefix": "cust"})
    with pytest.raises(GeneratorError, match="must be a mapping"):
        build("cust_0001")


# -- validation happens at load, not at run --------------------------

@pytest.mark.parametrize("spec", [
    {"generator": "choice"},
    {"generator": "choice", "options": []},
    {"generator": "choice", "options": "abc"},
    {"generator": "weighted", "options": {}},
    {"generator": "weighted", "options": {"a": 0}},
    {"generator": "weighted", "options": {"a": -1}},
    {"generator": "weighted", "options": ["a", "b"]},
    {"generator": "integer", "min": 10, "max": 1},
    {"generator": "decimal", "min": 10, "max": 1},
    {"generator": "decimal", "min": 1, "max": 10, "scale": -1},
    {"generator": "id", "prefix": ""},
    {"generator": "reference", "from": ""},
    {"generator": "expression", "expression": "quantity *"},
    {"generator": "expression", "expression": "__import__('os')"},
    {"generator": "template", "pattern": "{unclosed"},
    {"generator": "template", "pattern": "}stray{"},
])
def test_a_malformed_declaration_fails_when_the_pack_is_read(spec):
    # Not when a tick runs it. A pack should fail before a single
    # database exists, with the declaration named.
    with pytest.raises(GeneratorError):
        build(spec)


@pytest.mark.parametrize("spec", [
    {"generator": "choice", "options": [1], "wieght": 2},
    {"generator": "id", "prefix": "c", "padding": 4},
])
def test_an_unrecognised_key_is_a_typo_and_is_refused(spec):
    # `minimum` for `min` silently ignored would produce a column full
    # of values from a range nobody asked for.
    with pytest.raises(GeneratorError, match="does not understand"):
        build(spec)


def test_a_missing_required_key_says_which():
    with pytest.raises(GeneratorError, match=r"needs \['max'\]"):
        build({"generator": "integer", "min": 1})


def test_both_kinds_of_mistake_are_reported_together():
    # `minimum` for `min` is one mistake that shows up as two problems.
    # Being told only about the missing key sends the author looking
    # for something they thought they had written.
    with pytest.raises(GeneratorError) as raised:
        build({"generator": "integer", "minimum": 1, "max": 10})
    message = str(raised.value)
    assert "needs ['min']" in message
    assert "does not understand ['minimum']" in message


# -- values ----------------------------------------------------------

def test_constant():
    assert generate({"generator": "constant", "value": "wc-completed"}) == "wc-completed"


def test_ids_are_readable_sequential_and_padded():
    context = make_context()
    generator = build({"generator": "id", "prefix": "cust"})
    assert [generator.value(context) for _ in range(3)] == [
        "cust_000001", "cust_000002", "cust_000003",
    ]


def test_id_counters_persist_across_generator_instances():
    # They live on the context, because two emissions in one event use
    # separate generator objects and must not both issue cust_000001.
    context = make_context()
    assert build({"generator": "id", "prefix": "c"}).value(context) == "c_000001"
    assert build({"generator": "id", "prefix": "c"}).value(context) == "c_000002"


def test_id_counters_are_kept_apart_by_prefix():
    context = make_context()
    assert build({"generator": "id", "prefix": "a"}).value(context) == "a_000001"
    assert build({"generator": "id", "prefix": "b"}).value(context) == "b_000001"


def test_id_width_is_adjustable():
    assert generate({"generator": "id", "prefix": "x", "width": 3}) == "x_001"


def test_now_is_the_simulated_clock_not_the_wall_clock():
    assert generate({"generator": "now"}) == NOW


def test_choice_draws_from_the_declared_options_only():
    options = ["card", "cash", "account"]
    context = make_context()
    generator = build({"generator": "choice", "options": options})
    drawn = {generator.value(context) for _ in range(60)}
    assert drawn <= set(options)
    assert len(drawn) == 3


def test_weighted_respects_its_proportions():
    context = make_context(seed=11)
    generator = build({"generator": "weighted", "options": {"card": 3.0, "cash": 1.0}})
    counts = {"card": 0, "cash": 0}
    for _ in range(4000):
        counts[generator.value(context)] += 1
    # 3:1 expected. Generous enough not to flake, tight enough that an
    # implementation ignoring the weights (which gives 1:1) fails.
    assert 2.4 < counts["card"] / counts["cash"] < 3.7


def test_integer_stays_inside_an_inclusive_range():
    context = make_context()
    generator = build({"generator": "integer", "min": 1, "max": 3})
    drawn = {generator.value(context) for _ in range(200)}
    assert drawn == {1, 2, 3}


def test_decimal_is_exact_and_quantised():
    # Decimal rather than float, for the reason the schema layer has no
    # float type: money from floats accumulates error that surfaces as
    # a reconciliation off by pennies.
    context = make_context()
    generator = build({"generator": "decimal", "min": "5.00", "max": "500.00"})
    for _ in range(50):
        drawn = generator.value(context)
        assert isinstance(drawn, Decimal)
        assert Decimal("5.00") <= drawn <= Decimal("500.00")
        assert drawn.as_tuple().exponent == -2


def test_decimal_scale_is_adjustable_for_engines_that_want_more():
    # WooCommerce stores totals as DECIMAL(26,8); accounting uses 4.
    context = make_context()
    generator = build({"generator": "decimal", "min": 0, "max": 1, "scale": 8})
    assert generator.value(context).as_tuple().exponent == -8


# -- references and arithmetic ---------------------------------------

def test_reference_copies_a_value_rather_than_linking_to_it():
    # The business point: a sale line's unit price is the product's
    # price AT THAT MOMENT, and must not move when the product's does.
    product = {"unit_price": Decimal("9.99")}
    context = make_context(picked={"products": product})
    generator = build({"generator": "reference", "from": "picked.products.unit_price"})
    copied = generator.value(context)
    product["unit_price"] = Decimal("999.99")
    assert copied == Decimal("9.99")


def test_expression_computes_over_the_row_being_built():
    context = make_context(row={"quantity": 3, "unit_price": Decimal("9.99")})
    assert generate({"generator": "expression",
                     "expression": "quantity * unit_price"}, context) == Decimal("29.97")


def test_generators_report_what_they_depend_on():
    # How load-time validation finds a declaration naming a column its
    # table does not have.
    assert build({"generator": "expression",
                  "expression": "quantity * unit_price"}).references() == {
        "quantity", "unit_price"}
    assert build({"generator": "reference", "from": "subject.store_id"}).references() == {
        "subject.store_id"}
    assert build({"generator": "template",
                  "pattern": "{carrier}{flight_number}"}).references() == {
        "carrier", "flight_number"}
    assert build({"generator": "constant", "value": 1}).references() == set()


# -- templates, and the vulnerability they avoid ---------------------

def test_a_template_composes_the_aviation_natural_key():
    # The case that made templates and expressions share one resolver.
    context = make_context(row={"carrier": "BA", "flight_number": "0117",
                                "scheduled_date": "2026-03-02"})
    assert generate({"generator": "template",
                     "pattern": "{carrier}{flight_number}/{scheduled_date}"},
                    context) == "BA0117/2026-03-02"


def test_a_template_reaches_every_namespace():
    context = make_context(subject={"store_id": "S1"}, row={"sale_id": "X9"})
    assert generate({"generator": "template",
                     "pattern": "{subject.store_id}-{sale_id}"}, context) == "S1-X9"


@pytest.mark.parametrize("pattern", [
    "{x.__class__}",
    "{x.__class__.__mro__}",
    "{x.__init__.__globals__}",
    "{x[0]}",
    "{x!r}",
    "{x:>10}",
])
def test_a_template_cannot_walk_the_object_graph(pattern):
    # THE reason this does not use str.format. Format strings reach
    # attributes and items, so a pattern like these would walk to
    # module state. Here they either fail to match the placeholder
    # pattern at all (and are rejected as malformed) or resolve through
    # EvaluationContext, where every lookup is a dict access.
    context = make_context(row={"x": "value"})
    with pytest.raises((GeneratorError, ReferenceError_)):
        generate({"generator": "template", "pattern": pattern}, context)


def test_a_template_with_no_placeholders_is_just_a_string():
    assert generate({"generator": "template", "pattern": "wc-completed"}) == "wc-completed"


def test_a_template_stringifies_whatever_it_resolves():
    context = make_context(row={"total": Decimal("12.50"), "count": 3})
    assert generate({"generator": "template",
                     "pattern": "{count}x{total}"}, context) == "3x12.50"


def test_a_template_naming_a_missing_field_fails_at_generation():
    context = make_context(row={})
    with pytest.raises(ReferenceError_, match="no field 'carrier'"):
        generate({"generator": "template", "pattern": "{carrier}"}, context)


# -- determinism -----------------------------------------------------

def test_the_same_seed_produces_the_same_values():
    # The property the whole run depends on: change one thing, see what
    # that one thing did.
    spec = {"generator": "decimal", "min": 0, "max": 100}

    def draw(seed):
        context = make_context(seed=seed)
        generator = build(spec)
        return [generator.value(context) for _ in range(5)]

    assert draw(7) == draw(7)
    assert draw(7) != draw(8)


# -- ids shared across an occurrence ---------------------------------

def test_an_occurrence_id_is_the_same_for_every_emission_in_one_occurrence():
    # THE thing that lets a parent and its children be linked. With
    # plain `id`, every emission draws a fresh number and a sale and
    # its lines could never agree on one.
    context = make_context()
    first = build({"generator": "occurrence_id", "prefix": "sale"})
    second = build({"generator": "occurrence_id", "prefix": "sale"})
    assert first.value(context) == second.value(context) == "sale_000001"


def test_a_new_occurrence_gets_a_new_id():
    # A context is built per occurrence, so the cache starting empty is
    # exactly the scope wanted -- with no clearing step to forget.
    shared_counters = {}
    generator = build({"generator": "occurrence_id", "prefix": "sale"})
    first = make_context()
    first.counters = shared_counters
    second = make_context()
    second.counters = shared_counters
    assert generator.value(first) == "sale_000001"
    assert generator.value(second) == "sale_000002"


def test_occurrence_ids_are_kept_apart_by_prefix():
    context = make_context()
    assert build({"generator": "occurrence_id", "prefix": "sale"}).value(context) \
        == "sale_000001"
    assert build({"generator": "occurrence_id", "prefix": "delivery"}).value(context) \
        == "delivery_000001"


def test_occurrence_ids_and_plain_ids_share_a_counter_per_prefix():
    # Deliberate: they draw from the same sequence, so a pack mixing
    # both for one prefix still produces unique values rather than two
    # independent sequences that collide.
    context = make_context()
    assert build({"generator": "id", "prefix": "x"}).value(context) == "x_000001"
    assert build({"generator": "occurrence_id", "prefix": "x"}).value(context) == "x_000002"


def test_an_occurrence_id_declaration_is_validated_like_any_other():
    with pytest.raises(GeneratorError, match="non-empty string"):
        build({"generator": "occurrence_id", "prefix": ""})
    with pytest.raises(GeneratorError, match="does not understand"):
        build({"generator": "occurrence_id", "prefix": "s", "padding": 4})


# -- prose -------------------------------------------------------------

def contexts(count=1, *, seed=7, **fields):
    """Several contexts sharing ONE stream.

    A fresh RandomSource per call would re-seed, so every draw would be
    identical -- which made a first version of the varies-in-length
    test fail for a reason that had nothing to do with the generator.
    """
    from simulator.rng import RandomSource

    rng = RandomSource(seed).stream("prose")
    made = []
    for _ in range(count):
        context = EvaluationContext(now=datetime(2026, 3, 1, tzinfo=UTC), rng=rng)
        for name, value in fields.items():
            context.set_field(name, value)
        made.append(context)
    return made


def context_with(**fields):
    return contexts(**fields)[0]


NOTES = ["Attended site at {arrived}.",
         "Customer reported {fault}.",
         "Replaced the thermostat and tested the flow.",
         "Advised on an annual service."]


def test_prose_reads_in_the_order_it_was_declared():
    # THE design decision. Real notes are a sequence -- somebody
    # arrived, diagnosed, fixed, advised -- and prose assembled by
    # picking at random reads as nonsense that happens to be
    # grammatical: "Replaced the thermostat. Attended site."
    generator = build({"generator": "prose", "sentences": NOTES,
                       "pick": {"min": 2, "max": 4}})
    # Compared by WHERE EACH SENTENCE LANDS IN THE TEXT, not by walking
    # the declared list and filtering. A first version did the latter,
    # which yields indices that are sorted by construction -- so it
    # passed against a generator that shuffled, and its control did not
    # fire.
    seen = 0
    for context in contexts(40, arrived="09:20", fault="no hot water"):
        written = generator.value(context)
        appearing = [(written.index(_lead(s)), index)
                     for index, s in enumerate(NOTES) if _lead(s) in written]
        assert len(appearing) >= 2, written
        by_position = [declared for _, declared in sorted(appearing)]
        assert by_position == sorted(by_position), written
        seen += 1
    assert seen == 40


def _lead(sentence: str) -> str:
    """The part of a sentence before any placeholder, for locating it."""
    return sentence.split("{")[0]


def test_prose_never_says_the_same_thing_twice():
    # A note that repeats itself is not a shorter note, it is a broken
    # one.
    generator = build({"generator": "prose", "sentences": NOTES,
                       "pick": {"min": 4, "max": 4}})
    written = generator.value(context_with(arrived="09:20", fault="a leak"))
    assert written.count("Advised on an annual service.") == 1


def test_prose_varies_in_length():
    generator = build({"generator": "prose", "sentences": NOTES,
                       "pick": {"min": 1, "max": 4}})
    lengths = {generator.value(context).count(".")
               for context in contexts(40, arrived="09:20", fault="a leak")}
    assert len(lengths) > 1, lengths


def test_prose_can_name_the_row_it_is_about():
    # What makes a note specific rather than filler.
    generator = build({"generator": "prose", "sentences": ["Called {who} back."]})
    assert generator.value(context_with(who="Okafor")) == "Called Okafor back."
    assert generator.references() == {"who"}


def test_prose_uses_every_sentence_when_no_count_is_given():
    generator = build({"generator": "prose", "sentences": NOTES})
    written = generator.value(context_with(arrived="09:20", fault="a leak"))
    assert written.count(".") == len(NOTES)


def test_prose_refuses_to_promise_more_sentences_than_it_has():
    # Silently capping would produce notes shorter than the pack asked
    # for, which is the kind of quiet disagreement nobody notices until
    # the data looks thin.
    with pytest.raises(GeneratorError, match="only 2 are declared"):
        build({"generator": "prose", "sentences": ["One.", "Two."],
               "pick": {"min": 1, "max": 5}})


def test_prose_refuses_a_malformed_declaration():
    for spec, message in (
        ({"sentences": []}, "non-empty list"),
        ({"sentences": "not a list"}, "non-empty list"),
        ({"sentences": ["  "]}, "non-empty string"),
        ({"sentences": ["Unmatched {brace."]}, "placeholder"),
        ({"sentences": ["One."], "pick": 0}, "at least 1"),
        ({"sentences": ["One.", "Two."], "pick": {"min": 2, "max": 1}},
         "may not exceed"),
        ({"sentences": ["One."], "pick": "some"}, "whole number"),
    ):
        with pytest.raises(GeneratorError, match=message):
            build({"generator": "prose", **spec})


def test_prose_is_reproducible():
    first = build({"generator": "prose", "sentences": NOTES, "pick": {"min": 1, "max": 4}})
    second = build({"generator": "prose", "sentences": NOTES, "pick": {"min": 1, "max": 4}})
    written = [first.value(c) for c in contexts(5, arrived="09:20", fault="a leak")]
    again = [second.value(c) for c in contexts(5, arrived="09:20", fault="a leak")]
    assert written == again


# -- choose ------------------------------------------------------------

DELIVERY = {
    "generator": "choose",
    "when": [
        {"if": "total >= 50", "then": {"generator": "constant", "value": "0.0000"}},
        {"if": "total >= 25", "then": {"generator": "constant", "value": "2.4900"}},
    ],
    "otherwise": {"generator": "constant", "value": "4.9900"},
}


@pytest.mark.parametrize("total,expected", [
    ("12.00", "4.9900"), ("25.00", "2.4900"), ("49.99", "2.4900"),
    ("50.00", "0.0000"), ("80.00", "0.0000"),
])
def test_a_rule_chooses_by_the_row(total, expected):
    # "Free delivery over 50" is a real business rule, and until this
    # existed no pack could express one -- so every classification was
    # either constant or random, and neither is a rule.
    generator = build(DELIVERY)
    assert generator.value(context_with(total=Decimal(total))) == expected


def test_the_first_matching_clause_wins():
    # Overlapping conditions are normal in real rules, and declared
    # order is how a pack says which takes precedence -- so reordering
    # them is a change in meaning rather than a tidy-up.
    reordered = {**DELIVERY, "when": list(reversed(DELIVERY["when"]))}
    assert build(DELIVERY).value(context_with(total=Decimal("80.00"))) == "0.0000"
    assert build(reordered).value(context_with(total=Decimal("80.00"))) == "2.4900"


def test_a_branch_may_be_any_generator():
    # A rule chooses between anything the vocabulary offers, not
    # between literals only -- which is what lets an unclassified row
    # still get a plausible value rather than a placeholder.
    generator = build({
        "generator": "choose",
        "when": [{"if": "owed > 1000",
                  "then": {"generator": "constant", "value": "credit-control"}}],
        "otherwise": {"generator": "weighted", "options": {"retail": 3, "trade": 1}},
    })
    assert generator.value(context_with(owed=Decimal(4000))) == "credit-control"
    drawn = {generator.value(c).strip()
             for c in contexts(30, owed=Decimal(0))}
    assert drawn <= {"retail", "trade"} and len(drawn) == 2


def test_a_rule_reports_everything_it_depends_on():
    # Both the conditions and the branches, or the loader cannot check
    # a rule's references and a missing column surfaces mid-run.
    generator = build({
        "generator": "choose",
        "when": [{"if": "owed > 0",
                  "then": {"generator": "reference", "from": "tier"}}],
        "otherwise": {"generator": "reference", "from": "fallback"},
    })
    assert generator.references() == {"owed", "tier", "fallback"}


def test_a_rule_must_say_what_happens_when_nothing_matches():
    # A rule with no fallback produces null whenever nothing matched,
    # and a null meaning "no rule applied" is indistinguishable from
    # one meaning "not known" -- the ambiguity a consumer cannot
    # resolve.
    with pytest.raises(GeneratorError, match="otherwise"):
        build({"generator": "choose",
               "when": [{"if": "owed > 0",
                         "then": {"generator": "constant", "value": "x"}}]})


def test_a_malformed_rule_is_refused():
    fallback = {"generator": "constant", "value": "x"}
    for when, message in (
        ([], "non-empty list"),
        ("not a list", "non-empty list"),
        ([{"if": "owed > 0"}], "exactly `if` and `then`"),
        ([{"if": "", "then": fallback}], "non-empty expression"),
        ([{"if": "owed >", "then": fallback}], "condition"),
        ([{"if": "owed.__class__", "then": fallback}], "condition"),
    ):
        with pytest.raises(GeneratorError, match=message):
            build({"generator": "choose", "when": when, "otherwise": fallback})
