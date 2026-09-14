# AGENTS.md

Operational instructions for AI agents working on elysium-sim.

Deliberately short, and containing only what you would get **wrong**
without being told. Everything else -- structure, dependencies, what
the code does -- read from the code.

The reasoning behind most of these rules lives in the Elysium project's
own `PRINCIPLES.md`, which this repository was split out of and whose
conventions it keeps. The rules that differ here are marked.

## What this is

A program that operates real PostgreSQL databases for a simulated
organization, changing both their contents and their structure over
time, so that a system which reads databases can be tested against data
that moves.

**It has no consumer-specific code and must never acquire any.** Elysium
is one possible reader of these databases; the simulator does not import
it, depend on it, or know it exists. The only contact is a socket. A
convenience that reaches across that line -- deriving tables from a
consumer's ontology, say -- is the specific mistake this design exists
to avoid, and it is tempting enough that an earlier prototype really did
make it.

## Commands

```sh
./lint.sh                             # ruff, mypy, vulture, lock drift
python -m pytest tests/ -q            # the suite that must pass
python -m pytest tests/ -q -m "not postgres"   # without a local server
```

Dependencies are locked. `requirements.txt` and `requirements-dev.txt`
carry bounds and are what a human edits; the `.lock` files are generated
and carry exact versions with hashes. After changing either:

```sh
uv pip compile requirements.txt --generate-hashes -o requirements.lock
uv pip compile requirements.txt requirements-dev.txt --generate-hashes \
  -o requirements-dev.lock
```

`./lint.sh` fails if they have drifted.

## PostgreSQL is a real dependency

**Different from the parent project, which needs no server.** Tests that
start a real instance are marked `postgres` and skip cleanly without
one. That is environmental, not a regression.

The simulator starts its own instances with `initdb` and `pg_ctl` -- it
never uses a system service, never needs root, and never touches a
cluster it did not create. Each simulated silo gets its own instance,
its own data directory under `var/`, and its own port.

## You do not push

You have no write access. Produce a patch and hand it over:

```sh
git format-patch origin/main..HEAD --stdout > /mnt/user-data/outputs/<n>.patch
```

Verify it applies to a **fresh clone of the real remote HEAD** and
passes lint and tests there before presenting it. A patch that only
works in your working copy is not done. Clear old patches from the
outputs folder first, or the user can apply a stale one.

**Confirm the previous patch landed before starting the next.** Compare
the remote against what you handed over with `git ls-remote origin
main`. A patch can fail to apply on the other side quietly, and if you
then reset to origin you silently move back past your own work.

## Verification that actually verifies

When you write a test for a guarantee, **break the guarantee and confirm
that test fails.** If it still passes, find out why before moving on.
This is not a formality; it has repeatedly caught tests that could not
fail.

Two real examples from the work that produced this codebase:

- A tick-size independence test compared two runs' equilibrium ratio and
  passed against a deliberately broken implementation, because scaling
  both competing rates identically leaves the ratio unchanged. Only the
  time to reach equilibrium differed, and the run was long enough to
  hide it. Fixed by asserting the analytic value instead.
- A test asserting "not every sale is attributed to a customer" used
  `0 < named < total` and passed with the attribution probability raised
  to 1.0, because a store with no eligible customers still produces
  unattributed sales. Fixed with a measured band.

**Run coverage on the files you touched, not the total.** An average
hides a file at 60%.

```sh
python -m coverage run -m pytest tests/ -q
python -m coverage report -m --include="<file you changed>"
```

**Say plainly when something cannot be tested**, rather than shipping a
test that passes vacuously. Write the reason where the test would have
been.

**Verify the claim, not just the change.** A commit message asserts
purpose, and no test disproves that. For each claim, have a way you
checked it or cut it. Numbers in a commit message are measurements, not
estimates.

## Do not add speculative code

If nothing calls it, do not write it. Vulture catches it; noticing first
is better. A function called only by its own test counts as speculative
-- two were deleted on exactly these grounds while building the engine.

## Simulations live in YAML, not Python

**The central design rule.** The engine is agnostic to what is being
simulated: adding a domain means writing a pack file, never a subclass.
If something cannot be expressed in the pack vocabulary, the answer is
to extend the vocabulary deliberately -- never to add an escape hatch
that evaluates arbitrary code.

The vocabulary is closed on purpose, following the same discipline as
the parent project's filter operators: a fixed set, validated at load,
with anything outside it rejected rather than approximated.

## Commit messages

Subject <= 72 characters, body wrapped at 72. Explain *why*, name what
was measured, and state what you got wrong. Validate before committing:

```sh
awk '{ if (length($0) > 72) print NR": "length($0)" chars" }' /tmp/msg.txt
```

## Files you should not hand-edit

`requirements.lock` and `requirements-dev.lock` are generated.
`vulture_whitelist.py` is append-only, and each entry needs a comment
saying why.

## Every non-trivial file carries its own notes

At the bottom, under an `AI-ONLY NOTES` banner: what is resolved and
kept for history, what is deferred and why. The test is whether someone
could reasonably ask "why isn't this built yet?" and find the honest
answer already sitting there.
