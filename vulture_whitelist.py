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
