"""
The part that decides WHEN to forget.

`tasks/016` built the mechanism and left the policy: `cached_files`
says what an evaluation cached, `forget_file` drops one entry without
dropping the rest, and nothing NOTICED a change. This is what notices.

Two halves, kept apart on purpose:

- **Bookkeeping.** `Watcher` records what each evaluated root
  depends on and decides what to forget when a path changes. It has
  no timers and no threads, and every step is a call - so a test
  drives it with no sleeps.
- **Noticing.** A source of change events calls `changed()` or
  `rescan()`. `rescan()` is here because it needs nothing but
  `os.stat`; an inotify source is a separate module over the same
  step.

Written against `EvalStateLike`, so one watcher serves an in-process
`AsyncEvalState` and a remote `RPCEvalState` alike.

## Why a SNAPSHOT and not the closure

`tasks/016` said a root's closure is the `cached_files` diff around
its `eval_file`. That is wrong for a file two roots share, and
`tasks/083` holds the measurement: the diff says what an evaluation
newly CACHED, not what it READ, so the second root to import a shared
file gets a diff that does not mention it. Forgetting that diff leaves
the root answering its old value, SILENTLY.

So a root records the whole `cached_files` set as it stood when it
finished. Everything the root read was cached by then, so the snapshot
is a superset of the closure and cannot miss.

It over-forgets, and the amount is not small: a root registered after
some file was already cached is implicated by a change to that file
even if it never read it. `tasks/083` measures this. The trade is
deliberate - the alternative is not cheaper, it is wrong - and at
worst it degenerates to forgetting the whole evaluation cache, which
still keeps the fetched flake inputs that `resetFileCache()` drops.

## What it cannot see

Three limits, and each is named here rather than left to be found.

A file read by `builtins.readFile` or `builtins.path` is in no cache,
so no snapshot holds it and no change to it invalidates anything. That
is libexpr's boundary, not this module's, and `EvalState.cached_files`
says the same thing.

A file DISCOVERED by an evaluation is stamped after that evaluation
read it, because nothing knew to watch it before. An edit landing in
between is recorded as the baseline, so no rescan fires for it. The
window is one evaluation wide and it only applies the FIRST time a
file is seen; a file already watched keeps its old stamp, which errs
the other way. Shrinking it further needs libexpr to say what it is
about to read, and it does not.

`rescan()` stats THIS machine. The bookkeeping and `changed()` treat a
path as a string and work over RPC unchanged, but a remote state's
files are on the server. A watcher that polls has to share a
filesystem with its evaluator; one driven by `changed()` does not.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from huggorm_generated.protocols import EvalStateLike, ValueLike

# Not every cache entry is a file. libexpr evaluates its own
# `derivation-internal.nix` out of an in-memory accessor, and it comes
# back as `«nix-internal»/derivation-internal.nix`. A watcher that
# tried to stat it would fail on every evaluation, so entries under a
# virtual accessor are recorded and never watched.
#
# Recognised by the marker rather than by a stat, because a stat
# cannot tell "not a file" from "deleted since" - and a deleted file
# is a change, which is the opposite conclusion.
VIRTUAL = "«"


def _watchable(paths: Iterable[str]) -> list[str]:
    """The entries that name something on a filesystem."""
    return [p for p in paths if not p.startswith(VIRTUAL)]


class Watcher:
    """Records what each root depends on, and forgets it when it changes.

    One watcher per `EvalState`, because a snapshot is only a snapshot
    if nothing else evaluates at the same time. `eval_file` here takes
    a lock and the state's own `eval_file` does not, so a caller who
    reaches past this object POISONS the bookkeeping - the root it
    evaluated is unrecorded, and the roots recorded before it get
    snapshots that do not mention its files.

    That is the affinity assumption this repo already makes, written
    down where it can be read rather than left as a property of the
    async layer.
    """

    def __init__(self, state: EvalStateLike) -> None:
        self._state = state
        # root path -> every cached file when that root finished.
        # A superset of the root's closure, and why is in the module
        # docstring.
        self._snapshots: dict[str, set[str]] = {}
        # watched path -> the mtime_ns and size last seen. Size as
        # well as mtime because a write inside one filesystem tick is
        # invisible to mtime alone, and a test writes fast.
        self._seen: dict[str, tuple[int, int]] = {}
        self._lock = asyncio.Lock()

    @property
    def roots(self) -> list[str]:
        """The roots this watcher evaluated, in no particular order."""
        return sorted(self._snapshots)

    def watching(self) -> list[str]:
        """Every real file any recorded root depends on.

        What a change source watches. Excludes the virtual cache
        entries, which is why it is not just the union of snapshots.
        """
        seen: set[str] = set()
        for files in self._snapshots.values():
            seen |= files
        return sorted(_watchable(seen))

    async def eval_file(self, path: str) -> ValueLike:
        """Evaluate a file and record what the state then held.

        The same answer as the state's own `eval_file`, including the
        cheap second time - recording a snapshot does not evaluate
        anything, it reads the cache's key set.
        """
        async with self._lock:
            try:
                value = await self._state.eval_file(path)
            except Exception:
                await self._drop_unrecorded()
                raise
            files = set(await self._state.cached_files())
            self._snapshots[path] = files
            # `setdefault`, so a file already watched KEEPS the stamp
            # it had. That is the safe direction, not an oversight: an
            # edit that lands while the evaluation runs then leaves
            # the stamp disagreeing with the disk, and the next
            # `rescan` over-forgets. Re-stamping here would record the
            # NEW file against the OLD cached value and never fire.
            for real in _watchable(files):
                self._seen.setdefault(real, _stamp(real))
            return value

    async def _drop_unrecorded(self) -> None:
        """Forget what a FAILED evaluation cached. Called with the lock.

        A failed evaluation still caches the files it read, and the
        cache is what answers next time - so the corrected file is
        never re-read and the root raises the OLD error forever.
        Measured in `tasks/087`: a file fixed after a syntax error
        raises that same syntax error until something forgets it, and
        nothing did.

        Nothing else could. A failed evaluation records no snapshot,
        so `changed()` can never implicate that root again - the
        wedge is permanent and silent, which is this repo's named
        failure mode with no absence to look at.

        What to forget is DERIVED and costs no extra call. Every file
        a successful evaluation cached is in that root's snapshot, so
        a file that is cached and in no snapshot was read by an
        evaluation that failed. Forgetting a good file would cost a
        re-read and nothing else; leaving a bad one costs the answer.
        """
        kept: set[str] = set()
        for files in self._snapshots.values():
            kept |= files
        cached = set(await self._state.cached_files())
        for stale in sorted(_watchable(cached - kept)):
            await self._state.forget_file(stale)

    async def changed(self, path: str) -> list[str]:
        """Forget every root that could depend on `path`. Returns them.

        The explicit step, and the one a change source calls. It does
        not stat anything: the caller has already decided the file
        changed, which is what makes this drivable by a test with no
        timing at all.

        A path no root depends on forgets nothing and returns an empty
        list, so a source that over-reports costs nothing but the call.
        """
        async with self._lock:
            return await self._forget(path)

    async def rescan(self) -> list[str]:
        """Stat every watched file, and act on the ones that moved.

        The dependency-free change source. It answers the roots it
        forgot, so a caller can log or re-evaluate them.

        A file that DISAPPEARED counts as changed. The evaluation that
        cached it read it, so its absence is a different answer - and
        `forget_file` on a path the state cannot read is safe, because
        the failure surfaces on the next evaluation rather than here.

        THIS MACHINE'S filesystem, which is the one limit that does
        not hold for the rest of the class. `changed()` and the
        bookkeeping work over RPC unchanged, because a path is just a
        string to them. `rescan` calls `os.stat`, so a remote state
        whose files live on the server is not what it measures. A
        watcher using it has to share a filesystem with the evaluator.
        """
        async with self._lock:
            # Stat everything first, then forget. `_forget` prunes
            # `_seen`, so acting inside the walk would step on the
            # collection it is walking.
            moved = [p for p, was in sorted(self._seen.items())
                     if _stamp(p) != was]
            forgotten: list[str] = []
            for real in moved:
                self._seen[real] = _stamp(real)
                forgotten += await self._forget(real)
            return sorted(set(forgotten))

    async def _forget(self, path: str) -> list[str]:
        """The policy. Called with the lock held.

        Forgets the UNION of every snapshot naming `path`, not the
        snapshots' intersection and not `path` alone. A root's
        snapshot holds its whole closure including the files between
        it and `path`, and forgetting only the ends would leave a
        middle file cached - which is the same silent staleness one
        level down.
        """
        doomed: set[str] = set()
        roots: list[str] = []
        for root, files in self._snapshots.items():
            if path in files:
                doomed |= files
                roots.append(root)

        # A forgotten root has no snapshot any more: the next
        # `eval_file` records a new one. Keeping the old would say the
        # root still depends on files it may no longer read.
        for root in roots:
            del self._snapshots[root]

        for gone in sorted(doomed):
            await self._state.forget_file(gone)

        # Stop watching what no root depends on any more. A state
        # meant to live for days would otherwise stat every file it
        # ever touched, forever, and a rescan's cost would only grow.
        keep: set[str] = set()
        for files in self._snapshots.values():
            keep |= files
        self._seen = {p: s for p, s in self._seen.items() if p in keep}
        return sorted(roots)


def _stamp(path: str) -> tuple[int, int]:
    """A file's identity for change detection, or (-1, -1) if absent.

    `st_mtime_ns` and `st_size` together. mtime alone misses a write
    that lands inside one filesystem timestamp tick, which a test does
    routinely and an editor does occasionally.

    Absence is a value rather than an exception, so a deleted file
    compares unequal to the stamp taken when it existed and a file
    that never existed compares equal to itself.
    """
    try:
        st = os.stat(path)
    except OSError:
        return (-1, -1)
    return (st.st_mtime_ns, st.st_size)

