"""
context.py  (the four things a pack file may refer to, and nothing else)

Every value a pack declares is either a literal, something drawn from
randomness, or a reference to something else in flight. This file
defines what "something else" may be, and the answer is deliberately
four things:

  subject   the row or entity this event is happening to
  emitted   rows this event has already produced, by table
  picked    rows a pick generator chose, by table
  row       the row currently being built

A bare name means `row`, because that is overwhelmingly the common
case -- `line_total` referring to `quantity` beside it.

Why a closed SET rather than a path expression. Without it, `from:`
becomes an arbitrary traversal, and the moment a pack can reach
anywhere the simulator has to keep everything reachable. Four
namespaces is the difference between a configuration format and a
query language embedded in YAML.

One reference language, not two. Expressions and templates both
resolve names through this file. That is a direct outcome of testing
the vocabulary against aviation before building it: a flight leg's
natural key is composed as `{carrier}{flight_number}/{scheduled_date}`,
which is string composition over the same fields arithmetic reaches
for. Two resolvers would have drifted -- one gaining a namespace the
other did not -- and pack authors would have had to remember which
half of the file they were in.

Aggregates over emitted, because an end-of-day roll-up needs them: a
sale's total is the sum of the lines just written for it. Four
functions, closed like everything else: sum, count, min, max.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from random import Random
from typing import Any

#: The only roots a reference may start from.
NAMESPACES = frozenset({"subject", "emitted", "picked", "row"})

#: The only aggregate functions available over `emitted`.
AGGREGATES = frozenset({"sum", "count", "min", "max"})


class ReferenceError_(Exception):
    """A reference could not be resolved.

    Named with a trailing underscore because `ReferenceError` is a
    Python builtin, and shadowing it inside a module that also
    evaluates expressions would be a genuinely confusing thing to
    debug.
    """


@dataclass
class EvaluationContext:
    """Everything a value may be computed from, at one moment."""

    now: datetime
    rng: Random
    #: The row or entity the event is happening to. None for events
    #: that are not per-anything.
    subject: Mapping[str, Any] | None = None
    #: Rows this event has already produced, keyed by table. Ordered,
    #: because a later emission referring to `emitted.sale_items` means
    #: the ones just written.
    emitted: dict[str, list[dict]] = field(default_factory=dict)
    #: Rows chosen by a pick generator, keyed by table.
    picked: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    #: The row being built. Mutated as each column is generated, which
    #: is what lets a later column refer to an earlier one.
    row: dict[str, Any] = field(default_factory=dict)
    #: Id counters by prefix. Held here rather than on a generator
    #: because they must persist across events and a context does not:
    #: the caller owns the dict and passes the same one in each time,
    #: which is also what lets a resumed run restore them.
    counters: dict[str, int] = field(default_factory=dict)
    #: Ids issued once per occurrence and reused by every emission in
    #: it, keyed by prefix. A context is built per occurrence, so this
    #: starting empty is exactly the scope wanted -- see
    #: OccurrenceIdGenerator for what it solves.
    occurrence_ids: dict[str, str] = field(default_factory=dict)

    def resolve(self, reference: str) -> Any:
        """Look up a dotted reference. Raises rather than returning None.

        A missing reference is a mistake in a pack file, and returning
        None would put it in a database column where it looks like
        legitimately absent data. Raising names the reference and what
        was available.
        """
        parts = reference.split(".")
        root = parts[0]
        if root not in NAMESPACES:
            # A bare name is the common case and means the row being
            # built. Checked after the namespaces so that a column
            # genuinely named `row` is still reachable as `row.row`.
            return self._from_row(reference)

        if root == "row":
            return self._from_row(".".join(parts[1:]) or reference)
        if root == "subject":
            return self._from_subject(parts[1:])
        if root == "picked":
            return self._from_picked(parts[1:])
        return self._from_emitted(parts[1:])

    # -- namespaces --------------------------------------------------

    def _from_row(self, name: str) -> Any:
        if name not in self.row:
            raise ReferenceError_(
                f"no field {name!r} in the row being built; so far it has "
                f"{sorted(self.row)}. A column may only refer to one declared "
                f"before it."
            )
        return self.row[name]

    def _from_subject(self, path: list[str]) -> Any:
        if self.subject is None:
            raise ReferenceError_("this event has no subject to refer to")
        if len(path) != 1:
            raise ReferenceError_(f"subject.{'.'.join(path)} is not a single field name")
        if path[0] not in self.subject:
            raise ReferenceError_(
                f"the subject has no field {path[0]!r}; it has {sorted(self.subject)}"
            )
        return self.subject[path[0]]

    def _from_picked(self, path: list[str]) -> Any:
        if len(path) != 2:
            raise ReferenceError_(
                f"picked.{'.'.join(path)} must name a table and a field, "
                f"as in picked.products.unit_price"
            )
        table, field_name = path
        if table not in self.picked:
            raise ReferenceError_(
                f"nothing was picked from {table!r}; picked so far: {sorted(self.picked)}"
            )
        chosen = self.picked[table]
        if field_name not in chosen:
            raise ReferenceError_(
                f"the row picked from {table!r} has no field {field_name!r}; "
                f"it has {sorted(chosen)}"
            )
        return chosen[field_name]

    def _from_emitted(self, path: list[str]) -> Any:
        # Two shapes, because a table may be named bare or qualified by
        # its silo -- and events finish their rows under the qualified
        # name, since two silos may both have a `sale_items`. So
        # `emitted.shop.sale_items.sum.line_total` has one more segment
        # than `emitted.sale_items.sum.line_total`, and both are valid.
        if len(path) in (3, 4):
            table = ".".join(path[:-2])
            function, field_name = path[-2], path[-1]
            if function not in AGGREGATES:
                raise ReferenceError_(
                    f"{function!r} is not an aggregate; available: {sorted(AGGREGATES)}"
                )
            return self._aggregate(table, function, field_name)
        raise ReferenceError_(
            f"emitted.{'.'.join(path)} must name a table, an aggregate and a field, "
            f"as in emitted.sale_items.sum.line_total or "
            f"emitted.shop.sale_items.sum.line_total"
        )

    def _aggregate(self, table: str, function: str, field_name: str) -> Any:
        if table not in self.emitted:
            raise ReferenceError_(
                f"nothing has been emitted to {table!r} by this event; "
                f"emitted so far: {sorted(self.emitted)}"
            )
        rows = self.emitted[table]
        if function == "count":
            return len(rows)
        missing = [index for index, row in enumerate(rows) if field_name not in row]
        if missing:
            raise ReferenceError_(
                f"rows {missing} emitted to {table!r} have no field {field_name!r}"
            )
        values = [row[field_name] for row in rows]
        if not values:
            # sum of nothing is zero; min and max of nothing are not
            # defined, and inventing a value would be worse than saying so.
            if function == "sum":
                return Decimal(0)
            raise ReferenceError_(f"cannot take {function} of an empty {table!r}")
        if function == "sum":
            return sum(values)
        return min(values) if function == "min" else max(values)

    # -- building ----------------------------------------------------

    def set_field(self, name: str, value: Any) -> None:
        """Record a generated column, making it referable by later ones."""
        self.row[name] = value

    def finish_row(self, table: str) -> dict:
        """Move the built row into `emitted` and start a fresh one."""
        completed = dict(self.row)
        self.emitted.setdefault(table, []).append(completed)
        self.row = {}
        return completed


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): expressions and templates share this resolver
# rather than each having their own. That came out of pressure-testing the
# vocabulary against aviation before building it: a flight leg's natural key is
# composed as "{carrier}{flight_number}/{scheduled_date}", which is string
# composition over exactly the fields arithmetic reaches for. Two resolvers
# would have drifted, and pack authors would have had to remember which half of
# the file they were in.
#
# RESOLVED: a missing reference raises rather than returning None. None would
# land in a database column looking like legitimately absent data, which is the
# worst way for a pack typo to surface.
#
# RESOLVED: the exception is ReferenceError_ with a trailing underscore.
# ReferenceError is a builtin, and shadowing it inside the package that also
# evaluates expressions would be a genuinely confusing thing to debug.
#
# RESOLVED: occurrence_ids is scoped by the context's own lifetime rather than
# by anything explicit. A context is built once per occurrence, so an id cached
# here is automatically shared by every emission in that occurrence and by no
# other -- which is the whole requirement, with no clearing step to forget.
#
# DEFERRED (known, intentional, not yet built): no time arithmetic. Aviation
# needs it -- every movement has scheduled, estimated, target and actual times,
# and the estimate is the scheduled time plus a delay. Deliberately not folded
# into the expression grammar, because mixing datetimes in makes the operators
# type-dependent: `+` would mean addition for numbers and an offset for times.
# It belongs in a dedicated generator where the intent is stated rather than
# inferred from the operand types.
#
# DEFERRED: `emitted` supports aggregates but not indexing -- there is no
# emitted.sale_items.0.sku. Nothing needs a positional reference yet, and
# adding one invites packs to depend on emission order in ways that are fragile.
