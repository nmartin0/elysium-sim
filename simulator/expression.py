"""
expression.py  (arithmetic in a pack file, with a grammar that is closed)

`line_total: {expression: "quantity * unit_price"}`

The decision this file represents, and it reverses one i argued for
twice. The alternative was a closed vocabulary -- `{generator: product,
of: [quantity, unit_price]}` -- on the grounds that a parser slides
toward function calls and conditionals and eventually toward eval.
That risk is real. It is also preventable, and the readability cost of
the alternative is not: field service needs `subtotal * (1 + tax_rate)`,
which in the vocabulary form is eight lines of nested YAML. The pack
file is the product here -- it is what someone modelling a business
reads and edits -- so this is not a nicety.

Why this is not "eval with guardrails". Nothing is executed. The text
is parsed with ast.parse into a tree, and the tree is then walked by
this module against a whitelist of node types. An unlisted node is
rejected before any value is computed. There are no calls, no
attribute access, no subscripts, no comparisons, no strings, no
comprehensions, no lambdas, no imports, no assignment -- because none
of those node types is in the list, not because they are filtered out
afterwards. The whitelist is short enough that a reviewer can read it in
ten seconds, which is the property that makes this defensible.

DECIMAL, not float. Numeric literals become Decimal and division is
Decimal division. A pack computing a line total in floats would
reintroduce exactly the error the schema layer refuses to allow in a
column, one layer up, where it is harder to see.

Division BY zero is an error, not infinity or NaN. A simulated
business that quietly writes NaN into a money column has produced data
no consumer can do anything sensible with.

Dotted references are rewritten before parsing, which is the least
obvious thing in this file and the most important. A pack needs to
write `subject.multiplier`, and that parses as ast.Attribute -- the
node type behind every classic sandbox escape, starting with
`().__class__.__bases__`. Admitting Attribute to the whitelist would
let a dotted path reach anywhere in the object graph, and the claim
that the whitelist is the grammar would stop being true.

So dotted references never reach the parser as attribute access. They
are recognised by a pattern anchored to the four namespace roots,
substituted for opaque placeholder names, and resolved through
EvaluationContext afterwards. Attribute stays banned, and anything
dotted that is not rooted in a namespace -- `quantity.__class__` --
remains an Attribute node and is rejected exactly as before.
"""

import ast
import re
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

from simulator.context import NAMESPACES, EvaluationContext

#: Every AST node type an expression may contain. Anything else is
#: rejected. This list is the grammar -- there is no second filter
#: elsewhere, and nothing is stripped or rewritten before evaluation.
ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,   # the wrapper ast.parse(mode="eval") produces
    ast.BinOp,        # a + b
    ast.UnaryOp,      # -a
    ast.Name,         # a reference, resolved through EvaluationContext
    ast.Load,         # the context in which a Name is read
    ast.Constant,     # a numeric literal -- see _constant for the check
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.USub,
    ast.UAdd,
    # Comparisons, so a CONDITION can be written. The branches they
    # choose between live in YAML -- see `choose` in generators.py --
    # because a conditional is the first step from a configuration
    # format toward a language, and a ternary buried in a string is
    # where that step stops being visible. Comparing is not that step;
    # hiding the branches was.
    ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.BoolOp, ast.And, ast.Or,
    ast.Not,
)

#: A dotted reference rooted in one of the four namespaces. Anchored to
#: those roots deliberately: the set is closed, so this cannot match a
#: path into anything else. `quantity.__class__` does not match, stays
#: an ast.Attribute, and is rejected.
_REFERENCE = re.compile(
    r"\b(?:" + "|".join(sorted(NAMESPACES)) + r")(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
)

#: Placeholder prefix for substituted references. Deliberately ugly so
#: it cannot plausibly collide with a column name, and checked anyway.
_PLACEHOLDER = "__simref"

#: Operators, mapped to what they do. A dict rather than a match
#: statement so that the set of operations is a value that can be
#: inspected and tested, rather than control flow.
_BINARY = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
}

_COMPARE = {
    ast.Eq: lambda left, right: left == right,
    ast.NotEq: lambda left, right: left != right,
    ast.Lt: lambda left, right: left < right,
    ast.LtE: lambda left, right: left <= right,
    ast.Gt: lambda left, right: left > right,
    ast.GtE: lambda left, right: left >= right,
}

_UNARY = {
    ast.USub: lambda value: -value,
    ast.UAdd: lambda value: +value,
}


class ExpressionError(Exception):
    """An expression was malformed, rejected, or could not be computed."""


def evaluate(source: str, context: EvaluationContext) -> Decimal:
    """Compute one expression against a context."""
    tree, references = parse(source)
    return _evaluate_node(tree.body, context, source, references)


def parse(source: str) -> tuple[ast.Expression, dict[str, str]]:
    """Parse and validate, without evaluating.

    Returns the tree and the placeholder-to-reference mapping.
    Separate from evaluate() so a pack can be checked at load time: a
    typo in an expression should fail before any database exists, not
    three hours into a backfill from inside a tick.
    """
    rewritten, references = _substitute_references(source)
    try:
        tree = ast.parse(rewritten, mode="eval")
    except SyntaxError as error:
        raise ExpressionError(f"{source!r} is not a valid expression: {error.msg}") from error
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise ExpressionError(
                f"{source!r} uses {type(node).__name__}, which is not allowed in an "
                f"expression. Expressions may contain names, numbers, + - * / and "
                f"parentheses, and nothing else."
            )
        if isinstance(node, ast.Constant):
            _check_constant(node, source)
    return tree, references


def _substitute_references(source: str) -> tuple[str, dict[str, str]]:
    """Replace dotted namespace references with opaque names.

    See the module note: this is what keeps ast.Attribute out of the
    grammar while still letting a pack write `subject.store_id`.
    """
    if _PLACEHOLDER in source:
        raise ExpressionError(
            f"{source!r} contains {_PLACEHOLDER!r}, which is reserved"
        )
    references: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        placeholder = f"{_PLACEHOLDER}{len(references)}"
        references[placeholder] = match.group(0)
        return placeholder

    return _REFERENCE.sub(replace, source), references


def names(source: str) -> set[str]:
    """Every reference an expression depends on, dotted ones included.

    Used by validation at load time: an expression naming a column its
    table does not declare is a pack error, and this is how it is found
    without running anything.
    """
    tree, references = parse(source)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(references.get(node.id, node.id))
    return found


def _check_constant(node: ast.Constant, source: str) -> None:
    # ast.Constant covers numbers, strings, bytes, None, True and
    # False. Numbers and strings belong here; bytes, None and booleans
    # do not -- True is 1 in disguise and None is a value no condition
    # should be comparing to by literal.
    #
    # Strings were excluded and are now admitted, for one reason:
    # almost every classification rule a real business has compares
    # text. `branch == "bristol"` is the shape, and without it a
    # condition can only ask about numbers, which rules out most of
    # what a pack would want to say.
    #
    # The original objection -- that a string constant would make `+`
    # mean concatenation -- is answered by refusing arithmetic on
    # strings outright rather than by refusing strings. That also
    # closes the sharper risk nobody had written down: with Mult in
    # the grammar, `"x" * 100000000` is a denial of service in eight
    # characters.
    if isinstance(node.value, bool) or not isinstance(node.value, int | float | str):
        raise ExpressionError(
            f"{source!r} contains the literal {node.value!r}; expressions may only "
            f"contain numbers and text."
        )


def _evaluate_node(node: ast.AST, context: EvaluationContext, source: str,
                   references: dict[str, str]) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        # Through str() so that 0.1 becomes Decimal("0.1") rather than
        # the binary float's true value, which is not 0.1.
        return Decimal(str(node.value))

    if isinstance(node, ast.Name):
        reference = references.get(node.id, node.id)
        resolved = context.resolve(reference)
        if isinstance(resolved, str):
            # A RESOLVED value is not the same as a written literal. A
            # driver hands back DECIMAL as a string, so `total * 2`
            # where total is "12.50" has to keep working -- that is a
            # real case with a test older than this paragraph, and a
            # first version of this broke it by treating every string
            # as text.
            #
            # So: numeric if it can be, text if it cannot. A quoted
            # literal is text by the author's intent and never goes
            # through here.
            try:
                return Decimal(resolved)
            except InvalidOperation:
                return resolved
        return _as_decimal(resolved, reference, source)

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _truth(_evaluate_node(node.operand, context, source, references))
        operation = _UNARY[type(node.op)]
        return operation(_evaluate_node(node.operand, context, source, references))

    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            # `1 < x < 10` chains, and Python's semantics for that are
            # not the ones most people expect from a config file. One
            # comparison per expression, and `and` to join them.
            raise ExpressionError(
                f"{source!r} chains comparisons; write them separately joined by "
                f"`and`, which means what it looks like"
            )
        left = _evaluate_node(node.left, context, source, references)
        right = _evaluate_node(node.comparators[0], context, source, references)
        if isinstance(left, str) != isinstance(right, str):
            # Python would happily answer `"5" > 3` with a TypeError and
            # `"5" == 5` with False, and the second is the dangerous
            # one: a rule that silently never matches looks like a rule
            # that never applies.
            raise ExpressionError(
                f"{source!r} compares text with a number, which is always false "
                f"rather than an error -- quote both sides or neither"
            )
        return _COMPARE[type(node.ops[0])](left, right)

    if isinstance(node, ast.BoolOp):
        values = [_evaluate_node(value, context, source, references)
                  for value in node.values]
        # Every operand evaluated, not short-circuited. A condition
        # referring to a column that does not exist should say so
        # whichever side of the `and` it is on, rather than being
        # reported or not depending on the data.
        if isinstance(node.op, ast.And):
            return all(_truth(value) for value in values)
        return any(_truth(value) for value in values)

    if isinstance(node, ast.BinOp):
        left = _evaluate_node(node.left, context, source, references)
        right = _evaluate_node(node.right, context, source, references)
        if isinstance(left, str) or isinstance(right, str):
            # Refused rather than concatenated or repeated. `"a" + "b"`
            # is a template's job, and `"x" * 100000000` would be a
            # denial of service in eight characters.
            raise ExpressionError(
                f"{source!r} does arithmetic on text; text can be compared but not "
                f"added, multiplied or divided"
            )
        # Named apart from the unary case above: sharing one local name
        # for a one-argument and a two-argument callable makes the type
        # of the variable ambiguous, which mypy is right to object to.
        binary = _BINARY[type(node.op)]
        try:
            return binary(left, right)
        except (DivisionByZero, ZeroDivisionError) as error:
            # Not infinity, and not NaN. A simulated business that
            # writes NaN into a money column has produced data no
            # consumer can do anything sensible with.
            raise ExpressionError(f"{source!r} divided by zero") from error
        except InvalidOperation as error:
            raise ExpressionError(f"{source!r} could not be computed: {error}") from error

    # Unreachable: parse() rejected anything not in ALLOWED_NODES, and
    # every allowed node that can appear in a body is handled above.
    raise ExpressionError(f"{source!r} contains an unhandled node {type(node).__name__}")


def _truth(value: Any) -> bool:
    """Whether a value counts as true, without Python's quirks.

    Decimal zero is false and every other number true, which is what a
    pack author expects. Nothing here relies on the emptiness of a
    string or a list, because a condition is the wrong place to learn
    that "0" is true and 0 is not.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal | int | float):
        return value != 0
    return bool(value)


def _as_decimal(value: Any, name: str, source: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise ExpressionError(f"{source!r}: {name} is {value!r}, which is not a number")
    if isinstance(value, int | float | str):
        try:
            return Decimal(str(value))
        except InvalidOperation as error:
            raise ExpressionError(
                f"{source!r}: {name} is {value!r}, which is not a number"
            ) from error
    raise ExpressionError(f"{source!r}: {name} is a {type(value).__name__}, not a number")


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): this reverses a decision argued for twice --
# that arithmetic should be a closed vocabulary of generators rather than a
# parsed expression. The safety argument was sound and the readability cost was
# understated: `subtotal * (1 + tax_rate)`, which field service needs, is eight
# lines of nested YAML in the vocabulary form. The pack file is the product, so
# readability is not a nicety. Safety is preserved by construction rather than
# by care: ALLOWED_NODES is the grammar, and an unlisted node is rejected before
# any value is computed.
#
# RESOLVED: _check_constant inspects the value, not just the node type.
# ast.Constant covers strings, bytes, None and booleans as well as numbers, so
# the node whitelist alone would admit "a" + "b" and True * 5.
#
# RESOLVED: numeric literals go through str() on the way into Decimal.
# Decimal(0.1) is the binary float's true value, which is not 0.1, and a pack
# author writing 0.1 means 0.1.
#
# RESOLVED: dotted references are substituted for placeholder names before
# parsing, rather than admitting ast.Attribute to the whitelist. Found by a test
# failing: `subject.multiplier * 2` parses as an Attribute, which is the node
# type behind every classic sandbox escape. Allowing it would have let a dotted
# path reach anywhere in the object graph and would have falsified the claim
# that ALLOWED_NODES is the grammar. The substitution pattern is anchored to the
# four namespace roots, which is a closed set, so `quantity.__class__` does not
# match, stays an Attribute, and is still rejected.
#
# RESOLVED: string literals are allowed, because almost every classification
# rule a real business has compares text and a condition that can only ask about
# numbers rules out most of what a pack would want to say. The original
# objection -- that `+` would become concatenation -- is answered by refusing
# arithmetic on text rather than by refusing text, which also closes a sharper
# risk nobody had written down: with Mult in the grammar, `"x" * 100000000` is a
# denial of service in eight characters.
#
# RESOLVED: comparing text with a number is an error rather than False. Python
# answers `"5" == 5` with False, and a rule that silently never matches looks
# exactly like a rule that never applies.
#
# RESOLVED, and the note it replaces called the design correctly: comparison and
# boolean operators exist now, and the BRANCHES they choose between are declared
# in YAML by the `choose` generator rather than as a ternary inside a string.
# The objection was never to comparing -- it was to hiding what a rule chooses
# between, which is the step from a configuration format toward a language.
# ast.IfExp is still absent, so a ternary remains unwritable.
#
# RESOLVED: comparison chains (`1 < x < 10`) are refused rather than supported.
# Python evaluates them with semantics most people do not expect from a config
# file, and `and` says what it looks like.
#
# RESOLVED: boolean operators do not short-circuit. A condition naming a column
# that does not exist should say so whichever side of the `and` it is on, rather
# than being reported or not depending on the data.
#
# DEFERRED: no modulo, power, or integer division. None has come up; each is one
# entry in ALLOWED_NODES and one in _BINARY when it does.
#
# DEFERRED: the result is always Decimal, so an expression cannot produce an
# integer for an INTEGER column. The write layer coerces, which works, but a
# pack cannot currently say "this is a count, not an amount".
