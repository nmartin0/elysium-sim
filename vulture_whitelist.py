# vulture_whitelist.py
#
# Vulture's own whitelist format: a sequence of bare name references,
# each marking a name as "used" for Vulture's purposes. Not ordinary
# executable Python, which is why pyproject.toml excludes it from Ruff.
#
# APPEND-ONLY, and every entry needs a comment saying why it is a false
# positive rather than dead code. An entry without a reason is
# indistinguishable from someone silencing a real finding.

# simulator/scheduler.py -- ScheduledEvent.sequence exists solely to make
# the heap totally ordered under dataclass(order=True). It is read by the
# generated __lt__, never by name, so Vulture cannot see the use. Without
# it, two events on one timestamp fall through to comparing their dict
# payloads and raise TypeError.
sequence

# simulator/silos/sqlite.py -- sqlite3's own documented way to get
# dict-like rows is assigning Connection.row_factory. Vulture sees an
# attribute written and never read, because the reading happens inside
# the C module.
row_factory

# simulator/silos/rest.py -- http.server dispatches by NAME. It calls
# do_GET for a GET request and log_message for every log line, neither
# through a reference Vulture can see, so both read as dead. The
# `format` parameter is part of log_message's signature in the base
# class and is deliberately ignored: the override exists precisely to
# silence http.server's default stderr logging, which would otherwise
# bury a test run's real output.
do_GET
log_message
format
