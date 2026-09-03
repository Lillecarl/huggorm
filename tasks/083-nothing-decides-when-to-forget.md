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
