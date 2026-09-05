# What a raised verbosity costs

**OPEN.** Named in `tasks/089` step 4 and not measured there.

## The question

`printMsg` gates on `nix::verbosity` in a MACRO, so the arguments are
lazy and a rejected message costs nothing (`logging.hh:327`). Raising
the global moves that: every site at or under the new level runs
`fmt(args)` and builds a string the tap may then drop.

`subscribe_logs(level=N)` raises it to N and never lowers, so a
process that once served a talkative subscriber formats at talkative
for the rest of its life.

## Why it is not urgent

Bounded by what a caller ASKED FOR, which is the difference between
this and the pin `tasks/089` rejected. Nobody asks, nothing moves.

And the sites are not where the work is. `tasks/089` counted them:
libexpr holds 4 literal `printMsg(` and 29 across the whole macro
family, against 228 tree-wide. `.scratchpad/probe_levels.py` showed a
whole file evaluation raising ONE record under lvlInfo.

nanopynix measured this on their workloads and stopped at `chatty`.
`tasks/089` refused to adopt that number, and still should.

## What to measure

One live-store workload, timed at `nix::verbosity` = lvlError against
lvlVomit. Not `dummy://`: the interesting sites are in libstore, and
that store reaches almost none of them - which is the same blindness
that let the daemon regression through.

If the difference is small, nothing changes and this file says so. If
it is large, the answer is probably to LOWER the global when the last
subscription that needed it goes - which is a race the monotonic rule
currently avoids, and would need its own argument.

## What this is NOT

A reason to pin. The pin costs the daemon, permanently, and
`tasks/089` records why.

Opened 2026-09-05, from `tasks/089` step 4.
