# Task tracker

One file per issue, named `NNN-short-name.md`. When done, append the
`.done` suffix to the filename instead of deleting:

    mv 001-runner-resolve-race.md 001-runner-resolve-race.md.done

Keep files short: problem, evidence, fix sketch. A closed file keeps
its original text and gains a "## Done" section, so the fix stays
readable next to what it fixed.

Findings reference two architectural reviews, 2026-08-23 and
2026-08-25. Where they disagree, the later one wins.

## Open, roughly by what blocks what

- 030 (attribute sets on the wire) is closed, which unblocks the
  evaluation server: an attrset is what Nix evaluation mostly hands
  back, and Realize now fetches one in a single round trip.
- 031 (recursive handle tracking) is closed. Identity mapping, the
  wire-value boundary, and idempotent grants on every inbound handle.
- 015 (the real-Nix spike) is the other direction, and everything it
  needs is now in place: a settled surface, a lifecycle that does not
  leak, and a build that lints and typechecks what it produces.
- 029 (TypedDicts for the protocol dicts) is a design question, not a
  defect - and probably wants a stage-by-stage refactor rather than an
  annotation change.
- 026 (typed proxy parameters) is a design question, not a defect. It
  is the one place the two locations genuinely disagree.
- 015 (real-Nix spike) substitutes into the surface 017 settled.
- 008 (transitive policy), 012 (test blind spots), 022 (proto field
  stability), and the derivation half of 025 are hardening. 022 grew
  twice: free-function requests number their fields positionally too,
  and the manifest's "schema": 1 is written by the generator and read
  by nobody.
- 035 (anyio, not asyncio) is half done: the suites are anyio, the
  library is not. The movable half is the runtime and the ping loop;
  the rest waits on 014, because grpclib is an asyncio library.
- 015 (real Nix) is the direction now. Building function values into
  the mock was the point where mock fidelity stopped paying: it was
  reimplementing libexpr to prove things libexpr already does.
- 034 (functions as values) waits on 015. Its analysis is about the
  Python surface, not the mock, so it survives intact - and against
  libexpr the formals are real.
- 032 (log callbacks) and 033 (primops in Python) are the two places
  the flow reverses: C++ calling into Python, on Nix's schedule and
  Nix's thread. Neither can be generated from a binding declaration,
  and 033 is the harder one - a primop runs inside evaluation, so it
  cannot hop threads, cannot await, and its arguments do not outlive
  the call.
- 014 (transport shims) and 016 (evaluation server) are the
  destinations. 016's lifecycle contract is settled and executable -
  a detached evaluator survives its creator's death and a successor
  claims it warm - so what is left of it is the part that needs a real
  evaluator: warm caches, the file graph, background evaluation.

Every build lints and typechecks the code its package owns, and
`nix run --file . check` does the whole tree in about a second (013).

## What the codegen emits

Per wrapped class, three forms plus the wire:

    Async<X>     in-process wrapper, owns the thread hop      (async_x.py)
    <X>Like      the protocol both implementations satisfy    (protocols.py)
    RPC<X>       client class over a handle                   (rpc.py)
    <X>Service   gRPC service, and <X>Msg for a wire-value    (grpc_schema.pb)

Plus one stub package describing the BINDINGS, so the types all of the
above name are not Any to a typechecker (cythonix_bindings-stubs/, 027).
That one is built from the unfiltered surface: the policy drops and the
hierarchy split are rules about the wrappers, not about the bindings.

A class that needs no wrapper gets none of the first three and keeps
its message: it crosses as itself. The smoke test holds the three
Python surfaces to each other by signature, not by isinstance.

## What the bindings now declare

Each marker moved down the stack because a layer above was carrying
the same knowledge by hand. The generator reads all of them:

    _threading   pool | affine            execution policy
    _wire        value | proxy            does it serialize
    _wire_fields message shape + helpers  HOW it serializes  (023)
    _binds       the pxd class it wraps   pxd <-> pyx link   (020)
    _async       False to exclude         generation opt-out

    _abstract    True for a generated base       inheritance   (018)
    _blocking    False if no method can wait     wrap or not   (025)

Module-level functions declare `_threading` and `_binds`. Every public
one is in the manifest either way; the policy decides only whether it
gets an async form and an rpc, and "pool" is the only legal one - no
instance, so no thread to be affine to (021).

Constructor signatures come from the pxd, which is the only place they
exist at all - Cython exposes no signature for __cinit__ (019).
