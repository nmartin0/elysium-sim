"""
loader.py  (assembling a pack, in the order its parts depend on)

WHAT IS LEFT HERE after the split is the assembly and nothing else:
read the file, then build the parts in the only order that works.
Silos first because every later name is anchored to one, then schemas,
curves, lifecycles and persistence, then seeding, then events, then
the migration timeline -- which is validated against the schema the
events were checked against.

That order is the real content of this file. Each section lives in its
own module now (silos, schemas, curves, lifecycles, seed, events,
exports, effects, migrations, references, scope, values), and the
reason they cannot simply be called in any sequence is that a pack is
a set of cross-references: an emission names a table, a table names a
silo, a migration names a column that an earlier migration may have
renamed.

EVERYTHING IS CHECKED BEFORE ANYTHING RUNS, which is the whole value.
A pack naming a column its table does not declare, a transition to a
state never defined, a curve with twenty-three hours -- every one is a
typo, and the useful place to say so is when the file is read, with
the path named, rather than three hours into a backfill from inside a
tick.

VALIDATION LIVES WITH LOADING rather than in its own module. They are
the same operation: there is no notion of a parsed-but-unchecked pack
here, because such a thing has no legitimate use. Splitting them would
create one, and something would eventually consume it.
"""

from pathlib import Path

import yaml

from simulator.spec.curves import _load_curves
from simulator.spec.events import _load_events
from simulator.spec.lifecycles import _load_lifecycles, _load_persistence
from simulator.spec.migrations import _load_migrations
from simulator.spec.model import (
    PackSpec,
)
from simulator.spec.schemas import (
    _load_schemas,
)
from simulator.spec.scope import EventContext, LoadContext
from simulator.spec.seed import _load_seed
from simulator.spec.silos import _load_silos
from simulator.spec.values import (
    PackError,
    _mapping,
    _string,
)

#: Silo kinds that can hold a schema. Derived from the dialects that
#: exist rather than listed again: a kind with no dialect cannot have
#: tables created in it, and saying so twice is how the two lists drift.


#: Suffixes accepted in a duration like `4h` or `30m`. Durations appear
#: as min_dwell on lifecycle transitions and read far better than a
#: count of seconds -- `min_dwell: 7d` against `min_dwell: 604800`.

#: The namespaces a generator may reference during seeding. A seed step
#: has no subject, nothing picked and nothing emitted, so only the row
#: being built is available.


def load_pack(path: Path) -> PackSpec:
    """Read and fully validate a pack file."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise PackError(str(path), f"is not valid YAML: {error}") from error
    if not isinstance(raw, dict):
        raise PackError(str(path), "must be a mapping at the top level")
    return load_spec(raw)


def _check_withheld_tables(silos: dict, schemas: dict) -> None:
    """A withheld table has to be one the silo actually has.

    Checked here rather than in silos.py because silos are read first
    and nothing at that point knows what tables there will be. A typo
    would otherwise withhold nothing at all -- the quietest possible
    failure, since the consumer simply reads everything and the pack
    author believes it could not.
    """
    for name, silo in silos.items():
        if not silo.withheld:
            continue
        schema = schemas.get(name)
        declared = {table.name for table in schema.tables} if schema else set()
        missing = sorted(set(silo.withheld) - declared)
        if missing:
            raise PackError(
                f"silos.{name}.withheld",
                f"names {missing}, which {name!r} does not declare; it has "
                f"{sorted(declared)}"
            )


def load_spec(raw: dict) -> PackSpec:
    """Validate an already-parsed pack, so tests need no file."""
    name = _string(raw, "pack", "pack")
    description = str(raw.get("description", ""))

    silos = _load_silos(_mapping(raw, "silos", "silos"))
    curves = _load_curves(raw.get("curves") or {})
    schemas = _load_schemas(raw.get("schemas") or {}, silos)
    _check_withheld_tables(silos, schemas)
    lifecycles = _load_lifecycles(raw.get("lifecycles") or {})
    persistence = _load_persistence(raw.get("lifecycles") or {}, schemas)
    seed_context = EventContext(pack=LoadContext(
        schemas=schemas, silos=silos, curves=curves,
        lifecycles=lifecycles, persistence=persistence))
    seed = _load_seed(raw.get("seed") or [], seed_context)
    events = _load_events(raw.get("events") or {}, seed_context.pack)

    migrations = _load_migrations(raw.get("migrations") or [], schemas)

    return PackSpec(name=name, description=description, silos=silos,
                    schemas=schemas, curves=curves, lifecycles=lifecycles,
                    persistence=persistence, seed=seed, events=events,
                    migrations=migrations)


# -- silos -----------------------------------------------------------


# -- curves ----------------------------------------------------------


# -- schemas ---------------------------------------------------------


# -- lifecycles ------------------------------------------------------


# -- seed ------------------------------------------------------------


# -- events ----------------------------------------------------------


# -- migrations ------------------------------------------------------


#: Every operation a pack may schedule, by the name it uses. Explicit
#: rather than derived from the class names, so the vocabulary a pack
#: writes is a decision rather than an accident of refactoring.
#:
#: Public because the interactive console builds operations from the
#: same words, and a second vocabulary meaning the same things would
#: be the worst of both.


# -- small helpers ---------------------------------------------------


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): validation lives here rather than in its own
# module. Loading and validating are the same operation -- there is no notion
# of a parsed-but-unchecked pack in this codebase, because such a thing has no
# legitimate use, and splitting them would create one that something eventually
# consumes.
#
# RESOLVED: _relational_kinds() derives from dialects rather than listing kinds
# again. A silo with no dialect cannot have tables created in it, and writing
# that fact in two places is how the two lists drift.
#
# RESOLVED: seed generators are checked against the namespaces available during
# seeding, which is only `row`. A seed step has no subject and nothing emitted,
# so `{from: subject.store_id}` is a pack error catchable at load. This is the
# clearest use of generators.references() and the reason it exists.
#
# DEFERRED (known, intentional, not yet built): a non-null column with a
# database default still has to be generated, because the schema layer has no
# notion of defaults. That is a schema-layer gap rather than a loader one.
#
# DEFERRED: no check that a seed step's declared count is reachable -- a pack
# asking for 500 rows into a table with a primary key generated by `choice`
# over three options will fail at insert, not at load. Detecting it means
# reasoning about generator cardinality, which is a real analysis and not worth
# it until a pack hits the problem.
#
# DEFERRED: _string()'s path handling is slightly awkward (it appends the key
# unless the key already appears in the path) because some callers pass a path
# that already names the field. Worth tidying when a third pattern appears.
