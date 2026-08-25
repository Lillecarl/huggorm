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

- 018 (Store hierarchy) and 017 (protocol over async + RPC) shape the
  surface the real-Nix spike (015) would substitute into.
- 021 (free functions) is the largest uncovered part of the goal:
  complete surface coverage from the binding specifications. Real
  libstore keeps a lot of surface there - openStore, parseStorePath,
  computeFSClosure.
- 008 (transitive policy), 012 (test blind spots), 013 (lint and
  typecheck), 022 (proto field stability) are hardening.
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

Constructor signatures come from the pxd, which is the only place they
exist at all - Cython exposes no signature for __cinit__ (019).
