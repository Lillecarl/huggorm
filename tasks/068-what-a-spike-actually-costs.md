# What a spike actually costs

**OPEN.** Measured after the jj-workspace integration landed, because
"GBs of junk" needs a number before it needs a policy. The junk is
not the worktrees.

## Measured on 2026-09-01

    2.5 MB    one jj workspace checkout   (removed on ExitWorktree)
     23 MB    .jj, the whole repository's own store
    1.8 GB    1792 DEAD huggorm store paths
             21763 dead store paths in all
       82%    /nix/store, 9.6 GB free of 61 GB
        27    stale `spike:` heads from earlier sessions
     none     min-free / max-free in nix.conf

A workspace is 2.5 MB and it is deleted when the session leaves it. A
spike's three outputs are about 4 MB together:

    3.3 MB   huggorm-bindings      the compiled .so
    656 KB   huggorm-generated
    124 KB   bindings-src          the emitted C++

So a worktree costs almost nothing. What costs is the BUILDS a
worktree invites: every variation is a new derivation, none of them
rooted, and 1792 of them are sitting dead right now.

That is the same disk that filled and produced the SIGBUS inside
sqlite in `tasks/062`. 9.6 GB free is roughly two bad afternoons.

## Recommendations, ranked

### 1. Turn on nix's own automatic GC

`min-free` and `max-free` are unset, so nothing reclaims anything
until somebody runs `nix store gc` by hand. With them set the daemon
frees space DURING a build instead of the build dying:

    min-free = 5G
    max-free = 20G

This is the one that removes the `tasks/062` failure class rather
than measuring it. It lives in croshome, so it is Carl's.

### 2. Reclaim the 1.8 GB now

    nix store gc

21763 dead paths. It is global - it drops other projects' build
caches too - so it is a decision rather than a chore.

### 3. Iterate on the EMITTED C++, not on the compiled module

    nix build --file . bindings-src        seconds,  124 KB
    nix build --file . huggorm-bindings    minutes,  3.3 MB

Most emitter changes are checked by DIFFING the emitted C++ against
the previous store path. Compile only when the C++ actually changed
shape. Two findings this session came from that diff alone: the three
`@reads` accessors emitted byte-identical bodies, and 29 lambdas
gained a return type.

The exception is anything nanobind resolves at COMPILE or RUNTIME
rather than in the text - a missing type_caster emits fine, compiles
fine, and fails the signature gate (`tasks/067`).

### 4. Do not optimise the worktrees

2.5 MB, removed on exit. Leaving one open between sessions is
cheaper than rebuilding the spike inside it.

### 5. `discard_changes: true` is safe, and its name is wrong

It discards the CHECKOUT. The commits survive as an anonymous head -
verified: a file written in a workspace was still reachable by
`jj log -r 'files(...)'` after
`ExitWorktree(remove, discard_changes=true)`.

The remove hook takes a snapshot from INSIDE the workspace before
forgetting it, which is what makes that true. So there is no reason
to avoid the flag, and no reason to commit defensively before exiting.

### 6. Name every spike commit `spike:`, and sweep them

27 already exist and every one is findable:

    jj log -r 'description(glob:"spike:*")'

Sweeping them is a rewrite, so it wants Carl's word:

    jj abandon -r 'description(glob:"spike:*") ~ ::@'

They are small - the cost is that after a dozen spikes nothing says
which head was which experiment. The naming convention is what keeps
them legible, and it is already in use.
