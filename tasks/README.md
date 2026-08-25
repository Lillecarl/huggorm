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

- 019, 020 gate the real-Nix spike (015) in practice: typed Acquire
  and an explicit binding-to-C++ link.
- 018 (Store hierarchy) and 017 (protocol over async + RPC) shape the
  surface 015 would substitute into.
- 021 (free functions) is the largest uncovered part of the goal:
  complete surface coverage from the binding specifications.
- 008 (transitive policy), 012 (test blind spots), 013 (lint and
  typecheck), 022 (proto field stability) are hardening.
- 014 (transport shims) and 016 (evaluation server) are the
  destinations.
