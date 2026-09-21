"""
nodatabase.py  (the two silos that hold no databases, saying so)

A file drop is a folder and a REST service is a process; neither has
anything a `database:` could name. This refusal was written out twice,
byte for byte, which is two places for the message to drift and two
places to forget when a third such silo arrives.
"""

from simulator.silo import SiloError


def refuse_database(name: str, kind: str, database: str | None) -> None:
    """A silo of this kind holds no databases.

    Refusing rather than ignoring: a pack declaring one would otherwise
    have written something with no effect, and the author would have no
    way to find out.
    """
    if database is not None:
        raise SiloError(
            f"silo {name!r} is a {kind!r} silo and holds no database called {database!r}"
        )
