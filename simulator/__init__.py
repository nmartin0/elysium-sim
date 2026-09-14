"""
elysium-sim: a simulator that operates real PostgreSQL databases for a
simulated organization.

The databases are the only interface. Nothing in this package knows
what reads them, and nothing in it may ever import a consumer -- see
AGENTS.md for why that constraint is the point rather than a
restriction.
"""
