"""
Lets the simulator run straight from a source tree:

    python -m simulator check packs/field_service.yaml

`pip install -e .` gives the same thing as a `simulator` command. This
module exists so the tool works BEFORE that, because the first thing
anyone does with a checkout is try to run it.
"""

import sys

from simulator.cli import main

if __name__ == "__main__":
    sys.exit(main())
