"""
generators.py  (how a column gets a value, declared rather than coded)

The largest hierarchy in the project and the clearest case for one: a
stable contract -- produce a value for this column, in this context --
with implementations that have nothing in common internally. Drawing a
weighted choice and composing a template string share no code and
never will.

Every generator validates itself at load. from_spec() is separate from
value() on purpose. A pack declaring `{generator: choice}` with no
options, or a misspelled key, should fail when the pack is read --
before a single database exists -- rather than three hours into a
backfill from inside a tick, with nothing naming the file it came
from.

Unknown keys are an error, not ignored. `{generator: integer, minimum:
1, max: 10}` is a typo for `min`, and silently ignoring it produces a
column full of values from a range nobody asked for. Every from_spec
checks for keys it does not recognise, which is the difference between
a format that catches mistakes and one that absorbs them.

Templates do not use str.format. That is the single most important
line in this file. `"{carrier}{flight_number}".format(**row)` looks
obvious and is a known vulnerability class: format strings reach
attributes and items, so a pattern containing `{x.__class__.__mro__}`
walks the object graph, and one containing `{x.__init__.__globals__}`
reaches module state. A pack file is not hostile input in the usual
sense, but the whole point of the closed grammar in expression.py is
that safety comes from construction rather than from trusting the
author -- and it would be undone by one convenient .format() call.
Templates resolve their own placeholders through EvaluationContext,
the same resolver expressions use.
"""

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from simulator.context import EvaluationContext
from simulator.expression import ExpressionError, evaluate, names, parse
from simulator.rng import sample_without_replacement, weighted_choice

#: A placeholder inside a template pattern. Deliberately permits the
#: dotted forms the reference language allows and nothing else -- no
#: format specs, no conversions, no indexing.
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")


class GeneratorError(Exception):
    """A generator declaration was malformed."""


class Generator(ABC):
    """Produces one column's value."""

    #: The name a pack file uses in `{generator: ...}`.
    name: ClassVar[str]

    @classmethod
    @abstractmethod
    def from_spec(cls, spec: dict) -> "Generator":
        """Build from a pack declaration, validating it completely."""

    @abstractmethod
    def value(self, context: EvaluationContext) -> Any:
        """Produce the value."""

    def references(self) -> set[str]:
        """Every reference this generator depends on.

        Used by load-time validation to catch a generator naming a
        column its table does not declare. Most generators depend on
        nothing, so the default is correct for them rather than a stub.
        """
        return set()


# -- helpers shared by from_spec implementations ----------------------

def _check_keys(spec: dict, name: str, *, required: frozenset[str] | set[str] = frozenset(),
                optional: frozenset[str] | set[str] = frozenset()) -> None:
    keys = set(spec) - {"generator"}
    missing = sorted(required - keys)
    unknown = sorted(keys - required - optional)
    # Both are reported together, because they usually have one cause.
    # `{generator: integer, minimum: 1, max: 10}` is missing `min` and
    # carries an unknown `minimum`, and being told only the first sends
    # the author looking for a key they thought they had written.
    problems = []
    if missing:
        problems.append(f"needs {missing}")
    if unknown:
        problems.append(f"does not understand {unknown}")
    if problems:
        raise GeneratorError(
            f"generator {name!r} " + " and ".join(problems)
            + f"; it takes {sorted(required | optional)}"
        )


def _sequence(spec: dict, key: str, name: str) -> list:
    value = spec[key]
    if not isinstance(value, Sequence) or isinstance(value, str) or not value:
        raise GeneratorError(f"generator {name!r}: {key} must be a non-empty list")
    return list(value)


# -- the generators ---------------------------------------------------

class ConstantGenerator(Generator):
    """The same value every time."""

    name: ClassVar[str] = "constant"

    def __init__(self, constant: Any) -> None:
        self.constant = constant

    @classmethod
    def from_spec(cls, spec: dict) -> "ConstantGenerator":
        _check_keys(spec, cls.name, required={"value"})
        return cls(spec["value"])

    def value(self, context: EvaluationContext) -> Any:
        return self.constant


class IdGenerator(Generator):
    """A readable, unique, run-stable id like `cust_000042`.

    Counter-based rather than random. Random ids collide eventually,
    and a run's ids should be readable by hand when someone is looking
    at a database trying to work out what the simulator did. The
    counters live on the context because they must persist across
    events, which a generator instance does too but a context does
    not -- so the context carries a reference to a store the caller
    owns.
    """

    name: ClassVar[str] = "id"

    def __init__(self, prefix: str, width: int = 6) -> None:
        self.prefix = prefix
        self.width = width

    @classmethod
    def from_spec(cls, spec: dict) -> "IdGenerator":
        _check_keys(spec, cls.name, required={"prefix"}, optional={"width"})
        prefix = spec["prefix"]
        if not isinstance(prefix, str) or not prefix:
            raise GeneratorError(f"generator {cls.name!r}: prefix must be a non-empty string")
        return cls(prefix, int(spec.get("width", 6)))

    def value(self, context: EvaluationContext) -> str:
        context.counters[self.prefix] = context.counters.get(self.prefix, 0) + 1
        return f"{self.prefix}_{context.counters[self.prefix]:0{self.width}d}"


class OccurrenceIdGenerator(Generator):
    """One id per occurrence, shared by every emission in it.

    The thing that lets a parent and its children be linked. With
    plain `id`, every emission draws a fresh number, so a sale and its
    lines could never agree on one. And the obvious alternative --
    have the lines refer to the sale's id -- cannot work either: a sale
    that totals its lines must be emitted after them, so its id does
    not exist while they are being built.

    Issuing the id once for the occurrence dissolves the ordering
    problem: both emissions ask for it, the first one to ask creates
    it, and the order they are declared in stops mattering. It is also
    what a real system has -- an order number exists before the order
    header row does.
    """

    name: ClassVar[str] = "occurrence_id"

    def __init__(self, prefix: str, width: int = 6) -> None:
        self.prefix = prefix
        self.width = width

    @classmethod
    def from_spec(cls, spec: dict) -> "OccurrenceIdGenerator":
        _check_keys(spec, cls.name, required={"prefix"}, optional={"width"})
        prefix = spec["prefix"]
        if not isinstance(prefix, str) or not prefix:
            raise GeneratorError(f"generator {cls.name!r}: prefix must be a non-empty string")
        return cls(prefix, int(spec.get("width", 6)))

    def value(self, context: EvaluationContext) -> str:
        if self.prefix not in context.occurrence_ids:
            context.counters[self.prefix] = context.counters.get(self.prefix, 0) + 1
            context.occurrence_ids[self.prefix] = (
                f"{self.prefix}_{context.counters[self.prefix]:0{self.width}d}"
            )
        return context.occurrence_ids[self.prefix]


class NowGenerator(Generator):
    """The simulated clock, never the wall clock."""

    name: ClassVar[str] = "now"

    @classmethod
    def from_spec(cls, spec: dict) -> "NowGenerator":
        _check_keys(spec, cls.name)
        return cls()

    def value(self, context: EvaluationContext) -> datetime:
        return context.now


class ChoiceGenerator(Generator):
    """One of a declared list, uniformly."""

    name: ClassVar[str] = "choice"

    def __init__(self, options: list) -> None:
        self.options = options

    @classmethod
    def from_spec(cls, spec: dict) -> "ChoiceGenerator":
        _check_keys(spec, cls.name, required={"options"})
        return cls(_sequence(spec, "options", cls.name))

    def value(self, context: EvaluationContext) -> Any:
        return context.rng.choice(self.options)


class WeightedGenerator(Generator):
    """One of a declared mapping, in proportion to its weight."""

    name: ClassVar[str] = "weighted"

    def __init__(self, weights: dict) -> None:
        self.weights = weights

    @classmethod
    def from_spec(cls, spec: dict) -> "WeightedGenerator":
        _check_keys(spec, cls.name, required={"options"})
        options = spec["options"]
        if not isinstance(options, dict) or not options:
            raise GeneratorError(f"generator {cls.name!r}: options must be a non-empty mapping")
        for key, weight in options.items():
            if not isinstance(weight, int | float) or isinstance(weight, bool) or weight <= 0:
                raise GeneratorError(
                    f"generator {cls.name!r}: the weight for {key!r} must be a positive "
                    f"number, got {weight!r}"
                )
        return cls(dict(options))

    def value(self, context: EvaluationContext) -> Any:
        return weighted_choice(context.rng, self.weights)


class IntegerGenerator(Generator):
    """A whole number in an inclusive range."""

    name: ClassVar[str] = "integer"

    def __init__(self, low: int, high: int) -> None:
        self.low = low
        self.high = high

    @classmethod
    def from_spec(cls, spec: dict) -> "IntegerGenerator":
        _check_keys(spec, cls.name, required={"min", "max"})
        low, high = int(spec["min"]), int(spec["max"])
        if low > high:
            raise GeneratorError(f"generator {cls.name!r}: min {low} is above max {high}")
        return cls(low, high)

    def value(self, context: EvaluationContext) -> int:
        return context.rng.randint(self.low, self.high)


class DecimalGenerator(Generator):
    """An exact decimal in a range, quantised to a scale.

    Decimal rather than float, for the reason the schema layer refuses
    to have a float type at all: a money column filled from floats
    accumulates error that surfaces as a reconciliation off by pennies.
    """

    name: ClassVar[str] = "decimal"

    def __init__(self, low: Decimal, high: Decimal, scale: int) -> None:
        self.low = low
        self.high = high
        self.scale = scale

    @classmethod
    def from_spec(cls, spec: dict) -> "DecimalGenerator":
        _check_keys(spec, cls.name, required={"min", "max"}, optional={"scale"})
        low, high = Decimal(str(spec["min"])), Decimal(str(spec["max"]))
        if low > high:
            raise GeneratorError(f"generator {cls.name!r}: min {low} is above max {high}")
        scale = int(spec.get("scale", 2))
        if scale < 0:
            raise GeneratorError(f"generator {cls.name!r}: scale {scale} cannot be negative")
        return cls(low, high, scale)

    def value(self, context: EvaluationContext) -> Decimal:
        span = self.high - self.low
        drawn = self.low + span * Decimal(str(context.rng.random()))
        return drawn.quantize(Decimal(1).scaleb(-self.scale))


class ReferenceGenerator(Generator):
    """A value copied from somewhere else in flight.

    The `from:` form. Copying rather than referencing is the point at
    a business level: a sale line's unit price is the product's price
    at that moment, frozen, and must not move when the product's price
    later does.
    """

    name: ClassVar[str] = "reference"

    def __init__(self, reference: str) -> None:
        self.reference = reference

    @classmethod
    def from_spec(cls, spec: dict) -> "ReferenceGenerator":
        _check_keys(spec, cls.name, required={"from"})
        reference = spec["from"]
        if not isinstance(reference, str) or not reference:
            raise GeneratorError(f"generator {cls.name!r}: from must be a non-empty string")
        return cls(reference)

    def value(self, context: EvaluationContext) -> Any:
        return context.resolve(self.reference)

    def references(self) -> set[str]:
        return {self.reference}


class ExpressionGenerator(Generator):
    """Arithmetic over other fields. See expression.py for the grammar."""

    name: ClassVar[str] = "expression"

    def __init__(self, source: str) -> None:
        self.source = source

    @classmethod
    def from_spec(cls, spec: dict) -> "ExpressionGenerator":
        _check_keys(spec, cls.name, required={"expression"})
        source = spec["expression"]
        if not isinstance(source, str):
            raise GeneratorError(f"generator {cls.name!r}: expression must be a string")
        # Parsed here, at load, so a malformed expression fails with
        # the pack rather than mid-run. Re-raised as a GeneratorError
        # with the cause attached: from a pack author's point of view
        # this is a malformed declaration, and making them catch two
        # exception types to find out their pack is wrong would be a
        # distinction that serves the implementation rather than them.
        try:
            parse(source)
        except ExpressionError as error:
            raise GeneratorError(
                f"generator {cls.name!r}: {error}"
            ) from error
        return cls(source)

    def value(self, context: EvaluationContext) -> Decimal:
        return evaluate(self.source, context)

    def references(self) -> set[str]:
        return names(self.source)


class TemplateGenerator(Generator):
    """A string composed from other fields.

    Aviation's natural key -- carrier, flight number and date composed
    into one identifier -- is why this exists and why it resolves
    through the same context expressions do. See the module note for
    why it does not use str.format.
    """

    name: ClassVar[str] = "template"

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern

    @classmethod
    def from_spec(cls, spec: dict) -> "TemplateGenerator":
        _check_keys(spec, cls.name, required={"pattern"})
        pattern = spec["pattern"]
        if not isinstance(pattern, str):
            raise GeneratorError(f"generator {cls.name!r}: pattern must be a string")
        leftover = _PLACEHOLDER.sub("", pattern)
        if "{" in leftover or "}" in leftover:
            # A brace that is not a well-formed placeholder is a typo,
            # and passing it through would put a literal brace in a
            # database column where it looks like corrupted data.
            raise GeneratorError(
                f"generator {cls.name!r}: {pattern!r} has an unmatched or malformed "
                f"placeholder. Placeholders look like {{field}} or {{subject.field}}."
            )
        return cls(pattern)

    def value(self, context: EvaluationContext) -> str:
        return _PLACEHOLDER.sub(
            lambda match: str(context.resolve(match.group(1))), self.pattern
        )

    def references(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self.pattern))


class ProseGenerator(Generator):
    """A few sentences a person would actually read.

    WHY A SIMULATOR NEEDS THIS AT ALL. Every column in every pack so
    far is an identifier, a number, a date or a short label -- so a
    consumer whose purpose is answering questions in language has
    nothing to read. Work-order notes, complaint descriptions and call
    summaries are on most real business tables, and their absence is
    the difference between a demonstration that works and one worth
    watching.

    SENTENCES ARE CHOSEN IN DECLARED ORDER, never shuffled, and that is
    the whole design. Real notes are a sequence -- somebody arrived,
    diagnosed, fixed, advised -- and prose assembled by picking at
    random reads as nonsense that happens to be grammatical:
    "Replaced the thermostat. Attended site." A pack declares its
    sentences in the order they would be written, and a shorter note is
    a subset of that order rather than a different order.

    Without replacement, for the same reason. A note that says the same
    thing twice is not a shorter note, it is a broken one.

    Each sentence resolves through the same context as `template`, so a
    note can name the customer or the part -- which is what makes it
    specific rather than filler.
    """

    name: ClassVar[str] = "prose"

    def __init__(self, sentences: tuple[str, ...], low: int, high: int) -> None:
        self.sentences = sentences
        self.low = low
        self.high = high

    @classmethod
    def from_spec(cls, spec: dict) -> "ProseGenerator":
        _check_keys(spec, cls.name, required={"sentences"}, optional={"pick"})
        sentences = spec["sentences"]
        if not isinstance(sentences, list) or not sentences:
            raise GeneratorError(
                f"generator {cls.name!r}: sentences must be a non-empty list"
            )
        for sentence in sentences:
            if not isinstance(sentence, str) or not sentence.strip():
                raise GeneratorError(
                    f"generator {cls.name!r}: every sentence must be a non-empty string"
                )
            leftover = _PLACEHOLDER.sub("", sentence)
            if "{" in leftover or "}" in leftover:
                raise GeneratorError(
                    f"generator {cls.name!r}: {sentence!r} has an unmatched or "
                    f"malformed placeholder"
                )

        low, high = _prose_range(spec.get("pick"), len(sentences), cls.name)
        return cls(tuple(sentences), low, high)

    def value(self, context: EvaluationContext) -> str:
        count = (self.low if self.low == self.high
                 else context.rng.randint(self.low, self.high))
        # Indices, then sorted -- which is how "a subset in declared
        # order" is expressed. Choosing sentences directly and sorting
        # the STRINGS would order them alphabetically, which is not an
        # order any narrative has.
        chosen = sorted(sample_without_replacement(
            context.rng, range(len(self.sentences)), count))
        return " ".join(
            _PLACEHOLDER.sub(lambda match: str(context.resolve(match.group(1))),
                             self.sentences[index])
            for index in chosen
        )

    def references(self) -> set[str]:
        found: set[str] = set()
        for sentence in self.sentences:
            found |= set(_PLACEHOLDER.findall(sentence))
        return found


def _prose_range(raw: Any, available: int, name: str) -> tuple[int, int]:
    """How many sentences to pick, defaulting to all of them."""
    if raw is None:
        return available, available
    if isinstance(raw, int) and not isinstance(raw, bool):
        low = high = raw
    elif isinstance(raw, dict) and set(raw) <= {"min", "max"} and raw:
        low, high = raw.get("min", 1), raw.get("max", available)
    else:
        raise GeneratorError(
            f"generator {name!r}: pick must be a whole number or {{min, max}}"
        )
    if not isinstance(low, int) or not isinstance(high, int) or isinstance(low, bool):
        raise GeneratorError(f"generator {name!r}: pick bounds must be whole numbers")
    if low < 1 or high < low:
        raise GeneratorError(
            f"generator {name!r}: pick must be at least 1 and min may not exceed max"
        )
    if high > available:
        # Silently capping would produce notes shorter than the pack
        # asked for, which is the kind of quiet disagreement nobody
        # notices until the data looks thin.
        raise GeneratorError(
            f"generator {name!r}: asks for up to {high} sentences but only "
            f"{available} are declared"
        )
    return low, high


class ChooseGenerator(Generator):
    """A value that depends on a condition.

    "Free delivery over 50" is a real business rule, and until this
    existed no pack could express one -- so every classification in a
    simulated business was either constant or random, and neither is a
    rule. A department that is always the same value is not a boundary;
    a department drawn at random is not one either, because nothing
    about the row explains it.

    THE BRANCHES ARE DECLARED, not buried in a string. A ternary would
    have been fewer lines and is the step where a configuration format
    starts becoming a language: the condition and its two outcomes
    collapse into one expression that has to be read carefully to see
    what it chooses between. Here they are three keys.

        generator: choose
        when:
          - if: "total >= 50"
            then: {generator: constant, value: "0.0000"}
        otherwise: {generator: constant, value: "4.9900"}

    Each branch is a whole generator, so a rule can choose between
    anything the vocabulary already offers -- a constant, a weighted
    draw, an expression -- rather than between literals only.

    FIRST MATCH WINS, and the clauses are tried in declared order.
    Overlapping conditions are normal in real rules and the order is
    how a pack says which takes precedence, so reordering them is a
    change in meaning rather than a tidy-up.
    """

    name: ClassVar[str] = "choose"

    def __init__(self, clauses: tuple[tuple[str, Generator], ...],
                 otherwise: Generator) -> None:
        self.clauses = clauses
        self.otherwise = otherwise

    @classmethod
    def from_spec(cls, spec: dict) -> "ChooseGenerator":
        _check_keys(spec, cls.name, required={"when", "otherwise"})
        raw = spec["when"]
        if not isinstance(raw, list) or not raw:
            raise GeneratorError(
                f"generator {cls.name!r}: when must be a non-empty list of "
                f"{{if, then}} clauses"
            )
        clauses = []
        for clause in raw:
            if not isinstance(clause, dict) or set(clause) != {"if", "then"}:
                raise GeneratorError(
                    f"generator {cls.name!r}: every clause needs exactly `if` and "
                    f"`then`, got {sorted(clause) if isinstance(clause, dict) else clause}"
                )
            condition = clause["if"]
            if not isinstance(condition, str) or not condition.strip():
                raise GeneratorError(
                    f"generator {cls.name!r}: `if` must be a non-empty expression"
                )
            try:
                parse(condition)
            except ExpressionError as error:
                raise GeneratorError(
                    f"generator {cls.name!r}: condition {condition!r} -- {error}"
                ) from error
            clauses.append((condition, build(clause["then"])))
        # `otherwise` is required, not optional. A rule with no fallback
        # produces null whenever nothing matched, and a null that means
        # "no rule applied" is indistinguishable from one that means
        # "not known" -- which is exactly the ambiguity a consumer
        # cannot resolve.
        return cls(tuple(clauses), build(spec["otherwise"]))

    def value(self, context: EvaluationContext) -> Any:
        for condition, generator in self.clauses:
            if _is_true(evaluate(condition, context)):
                return generator.value(context)
        return self.otherwise.value(context)

    def references(self) -> set[str]:
        found = set(self.otherwise.references())
        for condition, generator in self.clauses:
            found |= names(condition) | generator.references()
        return found


def _is_true(value: Any) -> bool:
    """Whether a condition's result counts as true.

    evaluate() returns a Decimal for arithmetic and a bool for a
    comparison, so a condition written as `balance` rather than
    `balance > 0` still means something sensible.
    """
    if isinstance(value, bool):
        return value
    return value != 0


#: Every generator a pack file may name. Explicit rather than
#: discovered by scanning: ten entries do not need a plugin mechanism,
#: and a greppable dict is what a reader wants when a pack names one
#: that does not exist.
GENERATORS: dict[str, type[Generator]] = {
    generator.name: generator
    for generator in (
        ConstantGenerator,
        IdGenerator,
        OccurrenceIdGenerator,
        NowGenerator,
        ChoiceGenerator,
        WeightedGenerator,
        IntegerGenerator,
        DecimalGenerator,
        ReferenceGenerator,
        ExpressionGenerator,
        TemplateGenerator,
        ProseGenerator,
        ChooseGenerator,
    )
}


def build(spec: dict) -> Generator:
    """Turn one column's declaration into a generator.

    The single entry point, so every generator goes through the same
    validation and an unknown name fails the same way wherever it
    appears.
    """
    if not isinstance(spec, dict):
        raise GeneratorError(f"a column declaration must be a mapping, got {spec!r}")
    if "generator" not in spec:
        raise GeneratorError(f"declaration {spec!r} does not say which generator to use")
    name = spec["generator"]
    if name not in GENERATORS:
        raise GeneratorError(
            f"no generator called {name!r}; available: {sorted(GENERATORS)}"
        )
    return GENERATORS[name].from_spec(spec)


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): TemplateGenerator does not use str.format, and
# this is the most important decision in the file. Format strings reach
# attributes and items, so "{x.__class__.__mro__}" walks the object graph and
# "{x.__init__.__globals__}" reaches module state -- a known vulnerability
# class. The closed grammar in expression.py exists so that safety comes from
# construction rather than from trusting the pack author, and one convenient
# .format() call would undo it. Placeholders are resolved through
# EvaluationContext, the same resolver expressions use.
#
# RESOLVED: every from_spec rejects keys it does not recognise. `{generator:
# integer, minimum: 1, max: 10}` is a typo for `min`, and ignoring it silently
# produces a column full of values from a range nobody asked for.
#
# RESOLVED: id counters live on the context rather than on the generator
# instance. They must persist across events; a generator instance happens to as
# well, but a resumed run needs to restore them from somewhere the caller owns.
#
# RESOLVED: OccurrenceIdGenerator exists because a limit of the event
# vocabulary turned up while writing the first pack -- with inserts only, a
# parent could not carry an id its children also had, since a parent that
# totals its children must be emitted after them. Issuing the id once for the
# occurrence dissolves the ordering problem entirely, and is smaller than the
# alternative (an UpdateEmission filling the total in afterwards).
#
# DEFERRED (known, intentional, not yet built): no PickGenerator -- choosing a
# row from a table or an entity from a lifecycle. Every pack needs it (a sale
# picks a product, a work order picks a technician) and it cannot be written
# yet, because it needs the world layer to ask WHERE the candidates come from.
# Writing it against a guess at that interface is exactly the speculation this
# project keeps deleting.
#
# DEFERRED: no time arithmetic generator, so aviation's estimated-equals-
# scheduled-plus-delay cannot be expressed. Deliberately kept out of the
# expression grammar -- mixing datetimes in makes `+` mean addition for numbers
# and an offset for times -- so it belongs here as an explicit `offset_from`
# generator. Not written until a pack declares one, so its shape is drawn from
# a real case.
#
# DEFERRED: no geometry generator, which the city-government pack needs for
# PostGIS point, line and polygon columns. That pack also needs a spatial
# dialect, and the two should be designed together rather than a generator
# emitting values nothing can store.
