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

- 017 (protocol over async + RPC) is next: 018 gave it a real base to
  mirror, and the open question there is now only about the REMOTE
  side's return types.
- 017 shapes the surface the real-Nix spike (015) would substitute
  into.
- 008 (transitive policy), 012 (test blind spots), 013 (lint and
  typecheck), 022 (proto field stability) are hardening. 022 grew:
  free-function requests number their fields positionally too.
- 014 (transport shims) and 016 (evaluation server) are the
  destinations.

## What the bindings now declare

Each marker moved down the stack because a layer above was carrying
the same knowledge by hand. The generator reads all of them:

    _threading   pool | affine            execution policy
    _wire        value | proxy            does it serialize
    _wire_fields message shape + helpers  HOW it serializes  (023)
    _binds       the pxd class it wraps   pxd <-> pyx link   (020)
    _async       False to exclude         generation opt-out

    _abstract    True for a generated base       inheritance   (018)

Module-level functions declare `_threading` (which is what opts them
into the surface) and `_binds`. "pool" is their only legal policy: no
instance, so no thread to be affine to (021).

Constructor signatures come from the pxd, which is the only place they
exist at all - Cython exposes no signature for __cinit__ (019).
