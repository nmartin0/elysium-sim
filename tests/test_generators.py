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
        "constant", "id", "now", "choice", "weighted",
        "integer", "decimal", "reference", "expression", "template",
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
