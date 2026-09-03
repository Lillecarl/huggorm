# Nothing decides when to forget

**OPEN.** Every piece the watcher needs exists. The watcher does not.

`tasks/016` built the mechanism in three parts, and they compose:

    cached_files()          what to watch
    the diff around         which files belong to which
      one eval_file
    forget_file(path)       drop one without dropping the rest

What is missing is the part that NOTICES. A caller must watch the
files itself and hand the closure back, so the "live-reloading
evaluation server" in `CLAUDE.md` is still a manual reload.

## The decision this needs, and it is Carl's

Where the watcher lives is not obvious, and the two answers lead to
materially different work.

- **In the RPC server.** The server already owns a state's lifetime,
  its sweeper and its thread. A watch is one more thing on a
  connection, and a client that reconnects finds the state already
  invalidated. It also means a watch cannot exist without a server -
  the in-process binding gets nothing.
- **In `huggorm`, under both surfaces.** A watcher over an
  `EvalStateLike` works in-process and over RPC. It has to route
  `forget_file` onto the state's OWN thread, which the async layer
  already does, and it has to decide what happens when a watch
  outlives the client that asked for it.

No C++ either way. `inotify` is a Python call, and the C++ half is
built.

## What it must get right

**Forget the CLOSURE, not the file.** Measured in `tasks/016` and
gated twice: the cache holds no edge from an importer to its import,
so forgetting the changed file alone leaves the importer answering its
old value SILENTLY. The watcher has to keep the closure it recorded at
evaluation time, and forget all of it.

**Skip what it cannot stat.** `cached_files` returns what the cache
holds, and one entry is not a file:
`«nix-internal»/derivation-internal.nix`. A watcher that calls
`inotify_add_watch` on it fails on every evaluation.

**A closure is only recoverable if one evaluation runs at a time.**
The diff around `eval_file` is the closure, and two concurrent
evaluations on one state mix theirs. The affine state this repo
already assumes is what makes the technique work; a watcher that
records closures depends on it, and should say so.

**`builtins.readFile` is invisible.** A data file an expression read
is in no cache, so no watch fires for it. A known limit, and it
belongs in the API's docstring rather than in a surprise.

## Why it is not "just inotify"

The hard part is not the syscall. It is the bookkeeping: a closure per
evaluated path, an inode that appears in several closures, an editor
that writes by rename so the watch is on the wrong inode, and a
forget that must run on the state's thread rather than the watcher's.

## After this

Background eager evaluation, which is the last third of `tasks/016`.
It needs this one first: eagerly re-evaluating an expression is only
useful if something knows the old answer is stale.

## 2026-09-03: the diff is not the closure

`tasks/016` said the closure is the `cached_files` diff around one
`eval_file`. Measured before designing anything, and it is WRONG in
the case that matters: a file two roots share.

    shared.nix = 40 + 2
    a.nix      = import shared.nix
    b.nix      = import shared.nix

    diff around a: [a.nix, shared.nix]
    diff around b: [b.nix]              <- shared.nix is NOT here

The diff says what an evaluation newly CACHED, not what it READ. `a`
cached `shared.nix` first, so `b` hit the cache and its diff missed
it. Then:

    (shared.nix edited to 1 + 1)
    forget b's own diff  ->  b answers 42

A watcher holding per-root diffs answers stale, silently, which is
this repo's named failure mode. Nothing in `016`'s gates could see it:
each test gets a fresh `tmp_path`, so no file is ever shared.

### The policy, measured rather than argued

Record the full `cached_files` SNAPSHOT taken after each root
evaluates. When a file changes, forget the union of every snapshot
that contained it.

Sound, and the reason is short: a snapshot taken after R evaluated
holds everything cached at that moment, and every file R read was
cached by the time R finished. So `snapshot[R]` is a superset of R's
closure. It cannot miss.

Measured with a third root added, and it works:

    snapshot[a] = [a, shared, «nix-internal»]
    snapshot[b] = [a, b, shared, «nix-internal»]
    snapshot[c] = [a, b, c, shared, «nix-internal»]

    (shared edited)
    roots implicated: a, b, c
    a -> 2  OK
    b -> 2  OK

### What it costs, stated rather than hidden

`c.nix` is `10 + 1`. It reads nothing but itself and it is forgotten
anyway, because it was evaluated after `shared.nix` was already
cached and a snapshot cannot tell a hit from a file never read.

So the cost grows with the order roots are registered in: a root
registered late is implicated by almost any change. At worst this
degenerates to "forget the whole eval cache", which is still strictly
better than `resetFileCache()` - the fetched flake inputs stay, and
they are the expensive half Carl named.

The exact answer needs libexpr to say which files ONE evaluation read.
It will not (`tasks/016`), and the two approximations available are
this one and the diff. The diff is cheaper and silently wrong, so it
is not a trade - it is the wrong answer.

### What follows for the design

- The watcher OWNS `eval_file` and serializes it. A snapshot is only
  a snapshot if nothing else evaluates at the same time, so calling
  the state's `eval_file` directly bypasses the bookkeeping. That is
  the affinity assumption made load-bearing, and it has to be said in
  the API rather than assumed.
- NOTICING is separate from BOOKKEEPING. The core takes an explicit
  step - "this path changed, act on it" - so a gate can drive it with
  no sleeps and no event timing. An inotify source is then a thin
  adapter over the same step, and its own commit.
- Watches go on parent DIRECTORIES, not files. Most editors save by
  rename, which replaces the inode and orphans a file watch.
