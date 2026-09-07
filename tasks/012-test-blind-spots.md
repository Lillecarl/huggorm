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
- ~~Unknown CLASS on Acquire (unknown handle is covered).~~ CLOSED
  2026-09-06, and the shape it describes was already gone. See below.
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

## "Unknown CLASS on Acquire" is closed, 2026-09-06

The blind spot was real and it had moved. This file describes a
server-side lookup: `Session/Acquire` took a class NAME and the
server resolved it. That interface is gone - construction lives on
each class's OWN service now, with its declared parameters - so an
unknown class is an unknown gRPC PATH and grpclib answers it before
any handler runs. There is nothing left to test on the server.

What is left is the CLIENT's check, in `RemoteClient.acquire`, and it
had no gate:

    spec = ACQUIRE.get(cls_name)
    if spec is None:
        raise ValueError(f"{cls_name!r} cannot be constructed remotely; "
                         f"the bindings offer {sorted(ACQUIRE)}")
    if len(args) < spec.required or len(args) > len(spec.args):
        raise TypeError(...)

### The class check: folded, not duplicated

`test_a_produced_class_refuses_remote_construction` already drove the
first branch with `PathInfo`, asserting `match="PathInfo"` and
nothing more. A second test was written for the unknown-name case and
then DELETED before committing: one refusal with two tests is the
fact stated twice, and the existing gate is where it belongs.

It covers both names now, and the pair is what makes it say
something. `PathInfo` is DECLARED, bound, and still refused - it
crosses as a VALUE, so there is no handle to construct into.
`Nonexistent` is the ordinary case. Either alone would leave "being
declared is not what decides this" unstated.

It also asserts the message NAMES what is acquirable. `match=` on the
class name passed for a message that said nothing else, and a refusal
that only says no is one somebody has to debug.

### The arity check: both ends, and both are load-bearing

`required` and `len(args)` are two different numbers. `Store` takes
0..1 and `EvalState` takes 1..1, so one gate covers an optional
argument and a mandatory one.

Proved by breaking each half separately:

    only the lower bound    DID NOT RAISE, on Store with an extra
    only the upper bound    DID NOT RAISE, on EvalState with none

A constructor's arguments cross exactly like a method's, so the codec
does not complain about extra ones - it simply does not read them.
This check is what notices.

### What the first perturbation could not do

Removing the `if spec is None` branch outright does not run: the
typechecker refuses the file, because that branch is what narrows
`Acquire | None` for the nine uses below it.

    huggorm/remote.py:361: error: Item "None" of "Acquire | None"
    has no attribute "required"  [union-attr]

So the perturbation had to be an unknown class quietly BECOMING
another class - `ACQUIRE.get(cls_name) or next(iter(ACQUIRE.values()))`
- which is the failure a missing check would actually produce. It
fails both class-check assertions. Worth recording: a check the
typechecker will not let you delete still needs a gate, because the
typechecker holds its SHAPE and not its meaning.
