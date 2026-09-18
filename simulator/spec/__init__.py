"""
A pack file, parsed and validated.

`load_pack(path)` is the only entry point anything outside this
package should need. It returns a PackSpec that is fully checked:
every silo kind exists, every schema belongs to a silo that can hold
one, every lifecycle transition targets a declared state, every curve
has twenty-four hours, and every seed generator refers only to things
that will exist when it runs.

There is no way to obtain an unvalidated pack, deliberately. See
loader.py for why.
"""

from simulator.spec.loader import load_pack, load_spec
from simulator.spec.migrations import MIGRATION_OPERATIONS, build_change
from simulator.spec.model import PackSpec, SeedStep, SiloSpec
from simulator.spec.values import PackError

__all__ = ["MIGRATION_OPERATIONS", "PackError", "PackSpec", "SeedStep", "SiloSpec",
           "build_change", "load_pack", "load_spec"]
