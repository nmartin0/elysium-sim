"""
schema.py  (what a table looks like, said once, for any engine)

SMALL ON PURPOSE. Everything here has a caller. A first draft carried
a full mutation API -- with_column, replacing_table and the rest --
written for drift operations this repository does not have yet, and it
was deleted: code whose only users are its own tests is speculative.
Schema went with it and has come back now that relational.py applies
one. The mutation API returns with the commit that drifts a schema.

Pure declarations. Nothing here opens a connection, renders SQL, or
knows which engine a table will end up in -- dialect.py does that. The
split exists because the same business schema can legitimately live on
PostgreSQL in one deployment and MariaDB in another, and a pack should
describe the business rather than the engine.

THE TYPE VOCABULARY IS NEUTRAL, AND THAT IS NEW. An earlier version of
this project used one engine's own type names directly and recorded
the decision as correct-for-now, with a note that it would become the
wrong shape the moment a second engine appeared. It has. TEXT means
something different to PostgreSQL and MariaDB, TIMESTAMPTZ does not
exist outside PostgreSQL, and BOOLEAN is a TINYINT(1) in disguise on
MySQL. A pack that said "TEXT" would be quietly writing PostgreSQL.

MONEY IS DECIMAL AND NEVER FLOAT. This is the single most consequential
choice in the file and it is not a style preference. A float cannot
represent 0.10, so a column of them accumulates error that shows up as
a reconciliation that is off by pennies -- exactly the bug a business
notices and a simulator should never introduce on its own account.
Real schemas agree: WooCommerce stores totals as DECIMAL(26,8),
accounting systems commonly use DECIMAL(19,4). So MONEY is a distinct
declaration with explicit precision and scale, and there is no FLOAT
type here at all. If a pack ever genuinely needs approximate numbers
-- a sensor reading, a percentage -- that is the point to add one, and
it should have to be asked for.

LENGTHS MATTER ON ONE ENGINE AND NOT THE OTHER. PostgreSQL's TEXT is
unbounded and idiomatic; MariaDB's VARCHAR needs a length and indexes
on TEXT need a prefix. So TEXT carries an optional length, used where
the engine wants one and ignored where it does not.
"""

from dataclasses import dataclass
from enum import Enum


class ColumnType(Enum):
    """The closed set of types a pack may declare.

    Small deliberately. Every entry is here because a small business's
    real schema needs it; nothing is here because an engine offers it.
    """

    TEXT = "text"
    INTEGER = "integer"
    BIGINT = "bigint"
    #: Exact decimal. The only numeric type for money -- see the
    #: module note on why there is no float.
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    #: An instant, stored with its offset where the engine can.
    TIMESTAMP = "timestamp"


@dataclass(frozen=True)
class Column:
    """One column, as the business would describe it."""

    name: str
    type: ColumnType
    nullable: bool = True
    primary_key: bool = False
    #: For TEXT, the maximum length where an engine wants one. None
    #: means unbounded, which PostgreSQL honours directly and MariaDB
    #: renders as TEXT rather than VARCHAR.
    length: int | None = None
    #: For DECIMAL, total digits and digits after the point. Required
    #: for DECIMAL: an unqualified decimal means different things on
    #: different engines, and on MySQL it silently becomes DECIMAL(10,0)
    #: -- which stores money rounded to whole units.
    precision: int | None = None
    scale: int | None = None

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            # Names are interpolated into DDL, because SQL placeholders
            # stand for values and never for identifiers. Restricting
            # them to plain identifiers is conservative and happens to
            # exclude every injection vector.
            raise ValueError(f"column name {self.name!r} is not a plain identifier")
        if self.primary_key and self.nullable:
            raise ValueError(f"column {self.name!r} is a primary key and cannot be nullable")

        if self.type is ColumnType.DECIMAL:
            if self.precision is None or self.scale is None:
                raise ValueError(
                    f"column {self.name!r} is DECIMAL and must state precision and scale; "
                    f"an unqualified decimal becomes DECIMAL(10,0) on MySQL, which stores "
                    f"money rounded to whole units"
                )
            if not 1 <= self.precision <= 65:
                raise ValueError(f"column {self.name!r}: precision {self.precision} is out of range 1-65")
            if not 0 <= self.scale <= self.precision:
                raise ValueError(
                    f"column {self.name!r}: scale {self.scale} must be between 0 and "
                    f"the precision {self.precision}"
                )
        elif self.precision is not None or self.scale is not None:
            raise ValueError(f"column {self.name!r}: precision and scale apply only to DECIMAL")

        if self.length is not None:
            if self.type is not ColumnType.TEXT:
                raise ValueError(f"column {self.name!r}: length applies only to TEXT")
            if self.length < 1:
                raise ValueError(f"column {self.name!r}: length {self.length} must be positive")


@dataclass(frozen=True)
class Table:
    """One table, as declared."""

    name: str
    columns: tuple[Column, ...]

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise ValueError(f"table name {self.name!r} is not a plain identifier")
        if not self.columns:
            raise ValueError(f"table {self.name!r} has no columns")
        seen = [column.name for column in self.columns]
        duplicates = sorted({name for name in seen if seen.count(name) > 1})
        if duplicates:
            raise ValueError(f"table {self.name!r} declares {duplicates} more than once")
        if sum(1 for column in self.columns if column.primary_key) > 1:
            # Composite keys are real and both engines support them
            # through a table constraint rather than a column one. No
            # pack needs one yet, and accepting the declaration while
            # emitting invalid DDL would be worse than refusing it.
            raise ValueError(f"table {self.name!r} declares more than one primary key column")

    def column(self, name: str) -> Column:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(f"table {self.name!r} has no column {name!r}")

    def primary_key(self) -> Column | None:
        for column in self.columns:
            if column.primary_key:
                return column
        return None


@dataclass(frozen=True)
class Schema:
    """Every table in one silo."""

    tables: tuple[Table, ...]

    def __post_init__(self) -> None:
        names = [table.name for table in self.tables]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"schema declares tables {duplicates} more than once")

    def table(self, name: str) -> Table:
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(f"no table {name!r} in this schema")


# Convenience constructors. Not sugar for its own sake: these are the
# shapes every pack writes, and spelling out precision and scale at
# each money column is exactly where someone eventually gets it wrong.

def money(name: str, *, nullable: bool = False, precision: int = 19, scale: int = 4) -> Column:
    """A currency amount. DECIMAL(19,4) by default, as accounting uses."""
    return Column(name=name, type=ColumnType.DECIMAL, nullable=nullable,
                  precision=precision, scale=scale)


def identifier(name: str, *, length: int = 64) -> Column:
    """A primary key made of text, as most business systems use."""
    return Column(name=name, type=ColumnType.TEXT, nullable=False,
                  primary_key=True, length=length)


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): the type vocabulary is neutral now, where an
# earlier version of this project used one engine's own type names. That was
# recorded at the time as correct-for-one-engine and wrong the moment a second
# appeared -- which it has. The second caller existing is what justifies the
# abstraction; writing it with one engine in hand would have been guessing.
#
# RESOLVED: there is no FLOAT type, and DECIMAL requires explicit precision and
# scale. A float cannot represent 0.10, so money in floats accumulates error
# that surfaces as a reconciliation off by pennies -- the exact bug a business
# notices, and one a simulator must never introduce on its own account. MySQL
# makes an unqualified DECIMAL into DECIMAL(10,0), which stores money rounded
# to whole units, so the requirement is enforced rather than defaulted.
#
# RESOLVED (kept for history): a Schema container and a mutation API --
# with_column, without_column, replacing_column, with_table, without_table,
# replacing_table -- written for the drift operations. Nothing in simulator/
# called any of it, which is this project's definition of speculative, and
# Vulture said so. Deleted rather than covered by tests of methods nothing
# uses; it comes back with the commit that adds drift, where each method will
# have a real caller and the position-preservation invariant that motivated
# replacing_column can be asserted against something that depends on it.
# Deleting the mutation API left Schema itself with no caller, which is how a
# speculative abstraction announces itself: remove the code written for the
# future and the container holding it turns out to be for the future too.
#
# DEFERRED (known, intentional, not yet built): no foreign keys, indexes, CHECK
# constraints, or composite primary keys. Each is real and each is
# declared-but-unused weight until a pack needs it. Foreign keys are the most
# likely first addition, and they would also make a dropped table's failure
# modes richer -- which is a reason to want them for drift testing specifically.
#
# DEFERRED: no auto-increment or sequence-backed keys, even though both engines
# have them and plenty of real schemas use them. Every id a pack generates is
# readable and deterministic by design, which is worth more here than matching
# the vendor exactly; revisit when a pack needs to simulate a system whose ids
# a consumer must not be able to predict.
#
# DEFERRED: no JSON column type. Both engines have one and modern SaaS schemas
# use them heavily. Not added until a pack does, because a JSON column also
# needs a story about what the simulator puts IN it.
