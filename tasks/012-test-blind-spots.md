# Test blind spots

**OPEN.** The file's own "Still open" section lists what is left: a
concurrent first-call on one handle, driven through the server.

Review finding 14.

## Closed 2026-08-25

- smoke_test's query_derivation manifest assertion compared a string
  against a list of dicts. Vacuous. Fixed, plus the missing control
  (RemoteStore must KEEP the method).
- RemoteObj.__getattr__ used next() with no default, so a missing
  method raised StopIteration. Now AttributeError, naming what the
  manifest does offer. Dunder lookups short-circuit instead of walking
  into manifest resolution.
- The double-release check tested the wrong handle: release() blanks
  handle_id, so the second call sent an empty id and the test asserted
  that releasing "" fails. It now releases the same id through a fresh
  object and asserts the id appears in the error.
- Neither suite ran in any build. Both run in huggorm's
  checkPhase now, grpcurl included. A green suite finally says
  something about the last commit.

## Still open

- Concurrent first-calls on ONE handle over the wire. smoke_test
  covers PoolRunner directly; nothing drives it through the server.
- Unknown CLASS on Acquire (unknown handle is covered).
- The invariant HandleTable.audit() checks is only driven by an ad-hoc
  randomized loop, not by anything committed. Worth a small property
  test over put/release/share/detach/claim/sweep: that loop is what
  would have caught the escrow double count immediately.
- Transitive policy enforcement has no case to test (see 008).

## 2026-08-25: the suites are pytest and anyio now

The structural half of this is addressed. What used to be three
scripts with a hand-rolled `check()` and one giant `main()` is now 73
tests with fixtures, which changes what a failure costs: one test
fails instead of stopping everything after it, and a single scenario
can be run on its own.

That matters more than it did. Against a mock a failure was an
exception; against real Nix (tasks/015) it can be a native crash, and
the server being a subprocess is what keeps such a crash from taking
the run with it.

The blind spots this file lists are about COVERAGE, not structure, and
they are still open.
