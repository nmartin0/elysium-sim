"""
The SQL layer: real PostgreSQL instances, the ports they listen on, and
the databases inside them.

Everything that knows PostgreSQL specifically lives here. There is
deliberately no dialect abstraction -- one engine, one implementation,
and a dialect ABC with a single subclass would be exactly the
speculative abstraction this project's rules forbid. If a second engine
ever arrives, this package is where the seam gets cut.
"""
