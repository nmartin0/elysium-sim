"""Tests for the reference language and the expression grammar.

The adversarial half matters most. An expression evaluator in a
configuration format is the obvious place for this project to grow a
hole, so the closure is tested by trying to break out of it rather
than only by checking that arithmetic works.
"""

from datetime import UTC, datetime
from decimal import Decimal
from random import Random

import pytest

from simulator.context import AGGREGATES, NAMESPACES, EvaluationContext, ReferenceError_
from simulator.expression import ExpressionError, evaluate, names, parse
from simulator.rng import RandomSource

NOW = datetime(2026, 3, 2, 10, 15, tzinfo=UTC)


def make_context(**kwargs) -> EvaluationContext:
    return EvaluationContext(now=NOW, rng=Random(1), **kwargs)


# -- the reference language ------------------------------------------

def test_a_bare_name_means_the_row_being_built():
    context = make_context(row={"quantity": 3})
    assert context.resolve("quantity") == 3
    assert context.resolve("row.quantity") == 3


def test_a_column_named_row_is_still_reachable():
    # `row` as a root shadows a column of that name, so the explicit
    # form has to keep working or the namespace would make a legal
    # column name unusable.
    context = make_context(row={"row": "R1"})
    assert context.resolve("row.row") == "R1"


def test_subject_is_the_thing_the_event_happens_to():
    context = make_context(subject={"store_id": "S1"})
    assert context.resolve("subject.store_id") == "S1"


def test_picked_names_a_table_and_a_field():
    context = make_context(picked={"products": {"unit_price": Decimal("9.99")}})
    assert context.resolve("picked.products.unit_price") == Decimal("9.99")


def test_emitted_aggregates_over_rows_already_written():
    context = make_context(emitted={"sale_items": [
        {"line_total": Decimal("10.00")},
        {"line_total": Decimal("2.50")},
    ]})
    assert context.resolve("emitted.sale_items.sum.line_total") == Decimal("12.50")
    assert context.resolve("emitted.sale_items.count.line_total") == 2
    assert context.resolve("emitted.sale_items.min.line_total") == Decimal("2.50")
    assert context.resolve("emitted.sale_items.max.line_total") == Decimal("10.00")


def test_the_namespace_set_is_closed():
    assert NAMESPACES == {"subject", "emitted", "picked", "row"}
    assert AGGREGATES == {"sum", "count", "min", "max"}


@pytest.mark.parametrize(("reference", "fragment"), [
    ("missing", "no field 'missing'"),
    ("subject.store_id", "no subject"),
    ("picked.products.price", "nothing was picked"),
    ("emitted.sales.sum.total", "nothing has been emitted"),
    ("picked.products", "must name a table and a field"),
    ("emitted.sales.total", "must name a table, an aggregate and a field"),
    ("emitted.sales.median.total", "not an aggregate"),
])
def test_an_unresolvable_reference_says_what_went_wrong(reference, fragment):
    # Raising rather than returning None, because None lands in a
    # database column looking like legitimately absent data -- the
    # worst way for a pack typo to surface.
    with pytest.raises(ReferenceError_, match=fragment):
        make_context().resolve(reference)


def test_a_row_only_sees_columns_declared_before_it():
    context = make_context()
    context.set_field("quantity", 3)
    assert context.resolve("quantity") == 3
    with pytest.raises(ReferenceError_, match="only refer to one declared before it"):
        context.resolve("unit_price")


def test_finishing_a_row_moves_it_into_emitted_and_clears_the_builder():
    context = make_context()
    context.set_field("sku", "A1")
    completed = context.finish_row("sale_items")
    assert completed == {"sku": "A1"}
    assert context.emitted["sale_items"] == [{"sku": "A1"}]
    assert context.row == {}


def test_summing_an_empty_emission_is_zero_but_min_is_an_error():
    # Inventing a minimum would be worse than saying there isn't one.
    context = make_context(emitted={"sale_items": []})
    assert context.resolve("emitted.sale_items.sum.line_total") == Decimal(0)
    with pytest.raises(ReferenceError_, match="empty"):
        context.resolve("emitted.sale_items.min.line_total")


def test_an_aggregate_over_rows_missing_the_field_names_them():
    context = make_context(emitted={"sale_items": [{"line_total": 1}, {"other": 2}]})
    with pytest.raises(ReferenceError_, match=r"rows \[1\].*no field 'line_total'"):
        context.resolve("emitted.sale_items.sum.line_total")


# -- arithmetic ------------------------------------------------------

def test_the_motivating_case():
    context = make_context(row={"quantity": 3, "unit_price": Decimal("9.99")})
    assert evaluate("quantity * unit_price", context) == Decimal("29.97")


def test_parentheses_and_the_case_that_decided_the_design():
    # `subtotal * (1 + tax_rate)` is what field service needs, and what
    # made the closed-vocabulary alternative eight lines of nested YAML.
    context = make_context(row={"subtotal": Decimal("100"), "tax_rate": Decimal("0.2")})
    assert evaluate("subtotal * (1 + tax_rate)", context) == Decimal("120.0")


def test_arithmetic_is_decimal_not_float():
    # 0.1 + 0.2 is 0.30000000000000004 in binary floating point. A pack
    # computing a line total in floats would reintroduce exactly the
    # error the schema layer refuses to allow in a column.
    context = make_context(row={"a": "0.1", "b": "0.2"})
    assert evaluate("a + b", context) == Decimal("0.3")
    assert evaluate("0.1 + 0.2", context) == Decimal("0.3")


def test_references_reach_every_namespace():
    context = make_context(
        subject={"multiplier": 2},
        picked={"products": {"unit_price": Decimal("5")}},
        emitted={"lines": [{"amount": Decimal("3")}]},
        row={"quantity": 4},
    )
    assert evaluate(
        "quantity * subject.multiplier * picked.products.unit_price "
        "+ emitted.lines.sum.amount",
        context,
    ) == Decimal("43")


def test_unary_minus_and_division():
    context = make_context(row={"a": Decimal("10"), "b": Decimal("4")})
    assert evaluate("-a", context) == Decimal("-10")
    assert evaluate("a / b", context) == Decimal("2.5")


def test_dividing_by_zero_is_an_error_not_nan():
    # A simulated business that writes NaN into a money column has
    # produced data no consumer can do anything sensible with.
    context = make_context(row={"a": Decimal("1"), "b": Decimal("0")})
    with pytest.raises(ExpressionError, match="divided by zero"):
        evaluate("a / b", context)


def test_a_non_numeric_reference_is_refused_with_its_name():
    context = make_context(row={"name": "Okafor Plumbing", "flag": True, "nothing": None})
    for field_name in ("name", "flag", "nothing"):
        with pytest.raises(ExpressionError, match=field_name):
            evaluate(f"{field_name} * 2", context)


def test_numeric_strings_are_accepted_because_databases_return_them():
    context = make_context(row={"total": "12.50"})
    assert evaluate("total * 2", context) == Decimal("25.00")


# -- the closure, tested adversarially --------------------------------

@pytest.mark.parametrize("attack", [
    "__import__('os').system('true')",
    "open('/etc/passwd').read()",
    "().__class__.__bases__",
    "quantity.__class__",
    "[x for x in range(10)]",
    "(lambda: 1)()",
    # A ternary stays out: it is the construct that collapses a rule
    # and both its outcomes into one string, which is the step from a
    # configuration format toward a language. Comparisons and booleans
    # were admitted alongside it and it was not, because the objection
    # was never to comparing -- it was to hiding what a rule chooses
    # between. The branches live in YAML now; see the `choose`
    # generator.
    "quantity if quantity else 0",
    "items[0]",
    "{'a': 1}",
    "print(1)",
    "quantity := 5",
    "f'{quantity}'",
])
def test_everything_outside_the_grammar_is_rejected(attack):
    # None of these is filtered out after parsing. Each fails because
    # its node type is not in ALLOWED_NODES, which is checked before
    # any value is computed.
    with pytest.raises(ExpressionError):
        parse(attack)


# `'a' + 'b'` moved out of this list: a string literal is a valid
# expression NODE now, so it parses and is refused at evaluation
# instead -- see test_text_cannot_be_done_arithmetic_to. True, None and
# bytes are still refused at parse time, which is where a literal that
# can never mean anything belongs.
@pytest.mark.parametrize("literal", ["True * 5", "None", "b'x'"])
def test_non_numeric_literals_are_rejected(literal):
    # The node whitelist alone would admit these: ast.Constant covers
    # strings, bytes, None and booleans as well as numbers, so the
    # VALUE has to be checked too.
    with pytest.raises(ExpressionError, match="only contain numbers|not allowed"):
        parse(literal)


def test_malformed_syntax_is_reported_as_such():
    with pytest.raises(ExpressionError, match="not a valid expression"):
        parse("quantity *")


def test_validation_happens_without_evaluating():
    # A typo should fail when a pack is loaded, before any database
    # exists -- not three hours into a backfill from inside a tick.
    # Parsing with no context and no values must therefore succeed.
    tree = parse("quantity * unit_price")
    assert tree is not None


def test_names_reports_what_an_expression_depends_on():
    # How load-time validation finds an expression referring to a
    # column its table does not declare.
    assert names("quantity * unit_price + 1") == {"quantity", "unit_price"}
    assert names("subject.multiplier * 2") == {"subject.multiplier"}
    assert names("picked.products.unit_price + quantity") == {
        "picked.products.unit_price", "quantity"}


def test_the_grammar_is_small_enough_to_read():
    # The property that makes this defensible rather than eval with
    # guardrails: if the list grows quietly, so does the attack
    # surface. It grew by 11, once, deliberately -- comparisons,
    # booleans and negation, so a condition could be written. Every
    # entry is a comparison or a connective; none of them calls,
    # subscripts or reaches for an attribute, which is what the attack
    # cases above actually test.
    from simulator.expression import ALLOWED_NODES

    assert len(ALLOWED_NODES) == 23


# -- dotted references, and why they do not reopen the hole ----------

@pytest.mark.parametrize("attack", [
    "quantity.__class__",
    "items.foo.bar",
    "().__class__",
])
def test_a_dotted_path_outside_the_namespaces_is_rejected_at_parse_time(attack):
    # THE property anchoring buys, and it has to be asserted at PARSE
    # time specifically. A first version accepted either ExpressionError
    # or ReferenceError_, which made it pass against an unanchored
    # pattern: with that, `quantity.__class__` is substituted, survives
    # parsing, and only fails later when resolution finds no such field.
    # Both raise something, so the looser test could not tell them apart
    # -- and the difference is exactly what matters, because parse-time
    # rejection is what lets a pack be validated before anything runs.
    with pytest.raises(ExpressionError):
        parse(attack)


def test_a_namespace_rooted_path_to_a_dunder_fails_at_resolution():
    # `subject.__class__` DOES match the pattern, because `subject` is a
    # namespace root. It is substituted, parses fine, and then fails
    # where it should: resolving a subject field is a dict lookup, and
    # there is no key of that name. Attribute access never happens.
    context = make_context(subject={"a": 1})
    with pytest.raises(ReferenceError_, match="no field '__class__'"):
        evaluate("subject.__class__", context)


def test_substitution_does_not_disturb_bare_names_or_numbers():
    context = make_context(subject={"rate": Decimal("2")}, row={"rows": Decimal("3")})
    # A bare name that merely CONTAINS a namespace word is untouched:
    # the pattern requires a dot immediately after the root.
    assert evaluate("rows * subject.rate", context) == Decimal("6")


def test_the_placeholder_prefix_is_reserved():
    with pytest.raises(ExpressionError, match="reserved"):
        parse("__simref0 + 1")


# -- conditions --------------------------------------------------------

@pytest.mark.parametrize("source,expected", [
    ("total >= 50", True),
    ("total > 62", False),
    ("total == 62", True),
    ("total != 62", False),
    ("total < 100", True),
    ("total <= 62", True),
    ("total >= 50 and items > 2", True),
    ("total >= 50 and items > 9", False),
    ("total >= 99 or items > 2", True),
    ("not (total >= 99)", True),
])
def test_a_condition_is_answerable(source, expected):
    # Comparing was never the objection. The step from a configuration
    # format toward a language is hiding what a rule chooses between,
    # and the branches live in YAML -- see the `choose` generator.
    assert evaluate(source, conditional_context()) is expected


def conditional_context():
    context = EvaluationContext(now=datetime(2026, 3, 1, tzinfo=UTC),
                                rng=RandomSource(1).stream("conditions"))
    context.set_field("total", Decimal("62.00"))
    context.set_field("items", Decimal(3))
    return context


def test_a_chained_comparison_is_refused():
    # Python evaluates `1 < x < 10` with semantics most people do not
    # expect from a config file, and `and` says what it looks like.
    with pytest.raises(ExpressionError, match="chains comparisons"):
        evaluate("1 < total < 100", conditional_context())


def test_a_boolean_operator_does_not_short_circuit():
    # A condition naming a column that does not exist should say so
    # whichever side of the `and` it is on, rather than being reported
    # or not depending on the data.
    with pytest.raises(ReferenceError_):
        evaluate("total < 1 and nonexistent > 0", conditional_context())
    with pytest.raises(ReferenceError_):
        evaluate("total >= 1 or nonexistent > 0", conditional_context())


def test_a_ternary_is_still_unwritable():
    # ast.IfExp stayed out of the whitelist, so the branches cannot be
    # collapsed back into the string.
    with pytest.raises(ExpressionError):
        parse("1 if total > 50 else 2")


def test_the_grammar_is_still_closed():
    for source in ("__import__('os')", "total.__class__", "[total]",
                   "total()", "lambda: 1"):
        with pytest.raises(ExpressionError):
            parse(source)


# -- text --------------------------------------------------------------

def test_text_can_be_compared():
    # Almost every classification rule a real business has compares
    # text. `branch == "bristol"` is the shape, and without it a
    # condition can only ask about numbers -- which rules out most of
    # what a pack would want to say.
    context = conditional_context()
    context.set_field("branch", "bristol")
    assert evaluate('branch == "bristol"', context) is True
    assert evaluate('branch != "bath"', context) is True
    assert evaluate('branch == "bath" or total > 1', context) is True


def test_text_cannot_be_done_arithmetic_to():
    # The original objection to string literals was that `+` would
    # become concatenation. Answered by refusing arithmetic on text
    # rather than by refusing text -- which also closes the sharper
    # risk: with multiplication in the grammar, `"x" * 100000000` is a
    # denial of service in eight characters.
    context = conditional_context()
    context.set_field("branch", "bristol")
    for source in ('branch + "x"', 'branch * 100000000', '"a" + "b"',
                   'branch / 2', 'total + "x"'):
        with pytest.raises(ExpressionError, match="arithmetic on text"):
            evaluate(source, context)


def test_comparing_text_with_a_number_is_an_error_not_false():
    # Python answers `"5" == 5` with False, and a rule that silently
    # never matches looks exactly like a rule that never applies.
    context = conditional_context()
    context.set_field("branch", "bristol")
    with pytest.raises(ExpressionError, match="compares text with a number"):
        evaluate("branch == 5", context)


def test_other_literals_are_still_refused():
    # Numbers and text only. True is 1 in disguise and None is a value
    # no condition should compare to by literal.
    for source in ("total == True", "total == None", "total == b'x'"):
        with pytest.raises(ExpressionError, match="literal"):
            parse(source)


def test_a_numeric_string_from_a_column_is_still_a_number():
    # A driver hands back DECIMAL as a string, so arithmetic on a
    # column has to keep working. A first version of the text support
    # treated every string as text and broke this -- which is the
    # difference between a RESOLVED value and a written literal: the
    # literal is text because the author quoted it, the column value is
    # a number the driver stringified.
    context = make_context(row={"total": "12.50", "branch": "bristol"})
    assert evaluate("total * 2", context) == Decimal("25.00")
    assert evaluate('branch == "bristol"', context) is True
    with pytest.raises(ExpressionError, match="arithmetic on text"):
        evaluate('branch * 2', context)
