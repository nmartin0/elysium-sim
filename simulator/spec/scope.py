"""
scope.py  (what a reference is allowed to see, and where)

A generator declares what it depends on -- `subject.customer_id`,
`picked.products.sku`, `emitted.shop.sales.count.sale_id` -- and
whether that means anything depends entirely on WHERE it was written.
A seed step has a subject only if it declares a `per`; an emission can
see what an earlier emission in the same event wrote, and not what a
later one will; and nothing during seeding has emitted anything at
all.

These two carry that answer. Named `scope` rather than `context`
because simulator/context.py already means the thing a generator
actually reads VALUES from at run time, and these are about what is
LEGAL to read when the pack is loaded -- the difference between a
checker and an environment.

THREADED AS ONE ARGUMENT RATHER THAN FIVE, which was the point of
introducing them. Before, _finish_event took ten parameters and
_load_emission nine, and three separate features -- silos, lifecycles,
persistence -- each meant editing six signatures to carry one new fact
from the top of the file to the bottom. That is the shape that makes
the sixth edit the one somebody gets wrong.
"""

from dataclasses import dataclass

from simulator.schema import Schema
from simulator.spec.model import Curve, SiloSpec


@dataclass(frozen=True)
class LoadContext:
    """Facts that are constant for a whole pack.

    Threaded as one argument rather than five. Before this,
    _finish_event took ten parameters and _load_emission nine, and
    three separate features -- silos, lifecycles, persistence -- each
    meant editing six signatures to carry one new fact from the top of
    the file to the bottom. That is not a style complaint: it is the
    shape that makes the sixth edit the one somebody gets wrong.
    """

    schemas: dict[str, "Schema"]
    silos: dict[str, SiloSpec]
    curves: dict[str, Curve]
    lifecycles: dict
    persistence: dict

@dataclass(frozen=True)
class EventContext:
    """What one event's emissions and effects may refer to.

    Separate from LoadContext because these change as an event is
    read: the subject depends on what the event is `per`, and what has
    been emitted grows with each emission. Keeping them apart is what
    stops "facts about the pack" and "facts about where we are" being
    one bag.
    """

    pack: LoadContext
    #: Columns of the table this event happens to, empty if it happens
    #: to nothing in particular.
    subject_columns: frozenset[str] = frozenset()
    #: Tables an earlier emission in this event has written to.
    emitted: frozenset[str] = frozenset()
    #: Name -> columns, for tables this emission picks a row from.
    picked: "frozenset[tuple[str, frozenset[str]]]" = frozenset()

    @property
    def has_subject(self) -> bool:
        """Whether `subject` resolves here.

        Derived rather than passed. It was a separate parameter in
        twelve places, and at every origin it was exactly
        `bool(subject_columns)` -- checked against all four before
        removing it. A second parameter that can only ever agree with
        the first is a second thing to get wrong.
        """
        return bool(self.subject_columns)

    @property
    def schemas(self) -> dict[str, "Schema"]:
        return self.pack.schemas

    def having_emitted(self, qualified: str) -> "EventContext":
        """The same context, one emission further along.

        Picks are dropped, because they belong to the emission that
        declared them: a later emission referring to what an earlier
        one picked would be reading a row that is no longer being
        built.
        """
        return EventContext(pack=self.pack, subject_columns=self.subject_columns,
                            emitted=self.emitted | {qualified})

    def picking(self, picked: dict[str, set[str]]) -> "EventContext":
        return EventContext(
            pack=self.pack, subject_columns=self.subject_columns,
            emitted=self.emitted,
            picked=frozenset((name, frozenset(columns))
                             for name, columns in picked.items()),
        )

    def picked_columns(self, name: str) -> frozenset[str] | None:
        for picked_name, columns in self.picked:
            if picked_name == name:
                return columns
        return None

    def about(self, subject_columns: set[str]) -> "EventContext":
        return EventContext(pack=self.pack,
                            subject_columns=frozenset(subject_columns),
                            emitted=self.emitted, picked=self.picked)
