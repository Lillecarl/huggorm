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

- 013 (lint and typecheck) is next. Every piece it needs now exists:
  027 gave the bindings stubs, 017 gave consumers protocols to be
  checked against, and a mypy run over a consumer is already clean and
  already catches the five mistakes it should. What remains is wiring
  it into the build and annotating _runtime.py, which is the cause of
  three quarters of the errors left in the emitted package. Note that
  the run needs --python-executable, not MYPYPATH: mypy honours a
  -stubs package only in a real site-packages (see 027).
- 026 (typed proxy parameters) is a design question, not a defect. It
  is the one place the two locations genuinely disagree.
- 015 (real-Nix spike) substitutes into the surface 017 settled.
- 008 (transitive policy), 012 (test blind spots), 022 (proto field
  stability), and the derivation half of 025 are hardening. 022 grew:
  free-function requests number their fields positionally too.
- 014 (transport shims) and 016 (evaluation server) are the
  destinations.

## What the codegen emits

Per wrapped class, three forms plus the wire:

    Async<X>     in-process wrapper, owns the thread hop      (async_x.py)
    <X>Like      the protocol both implementations satisfy    (protocols.py)
    RPC<X>       client class over a handle                   (rpc.py)
    <X>Service   gRPC service, and <X>Msg for a wire-value    (grpc_schema.pb)

Plus one stub package describing the BINDINGS, so the types all of the
above name are not Any to a typechecker (fake_library-stubs/, 027).
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

Module-level functions declare `_threading` (which is what opts them
into the surface) and `_binds`. "pool" is their only legal policy: no
instance, so no thread to be affine to (021).

Constructor signatures come from the pxd, which is the only place they
exist at all - Cython exposes no signature for __cinit__ (019).
