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

from simulator.spec.loader import PackError, load_pack, load_spec
from simulator.spec.model import PackSpec, SeedStep, SiloSpec

__all__ = ["PackError", "PackSpec", "SeedStep", "SiloSpec", "load_pack", "load_spec"]
