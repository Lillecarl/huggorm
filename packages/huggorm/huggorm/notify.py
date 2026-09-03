"""
The part that NOTICES, without being asked.

`watch.Watcher` decides what to forget when a path changes, and
`Watcher.rescan()` finds a change by stat-ing every watched file. This
is the other change source: the kernel tells us instead.

It is a thin adapter and that is the whole design. `tasks/083` kept
noticing separate from bookkeeping so a third caller of
`Watcher.changed()` would be an addition rather than a rewrite, and
this is that third caller. Nothing here decides what a change MEANS.

## Directories, not files

Watches go on the parent directory of each watched file, never on the
file. Most editors save by writing a temporary file and renaming it
over the original, which replaces the inode - and a watch follows the
inode, so a file watch survives the save while pointing at the file
nobody has any more. The rename is a `MOVED_TO` in the parent, so a
directory watch sees exactly the event a file watch misses.

It costs the events for every other file in that directory. Those are
dropped here rather than passed on: `changed()` on a path no root
depends on already answers nothing, but it takes the watcher's lock
to say so, and a directory holding a checkout raises thousands of
them.

## Linux only

inotify is a Linux interface. `asyncinotify` is the binding, chosen in
`tasks/083`: it is asyncio-native, so an adapter is a task that
iterates events, and it propagates nothing but `python3`. Darwin was
ruled out by Carl for the project's stage, which is what let a
cross-platform library lose to a smaller one.

`Watcher.rescan()` is still a complete change source and needs no
dependency at all, so nothing here is required to use a watcher.

## What it inherits, and what it adds

Every limit in `watch.py` still holds: a `builtins.readFile` is in no
cache and so in no watch, and the files are THIS machine's, so a
remote state's are on the server.

One limit is its own. A watch set is only correct for the roots that
had been evaluated when it was built, so `sync()` has to run after an
evaluation adds files. `Notifier.eval_file` does that for a caller;
calling `Watcher.eval_file` directly is still correct and just leaves
the watch set behind until the next `sync()`.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from asyncinotify import Inotify, Mask

if TYPE_CHECKING:
    from huggorm_generated.protocols import ValueLike

    from .watch import Watcher

# What counts as "this file is not what it was".
#
# MODIFY is the in-place write. MOVED_TO is the editor save, which is
# the case a file watch cannot see at all. CREATE and MOVED_FROM and
# DELETE cover a file appearing and leaving, both of which are
# different answers than the one an evaluation cached.
#
# DELETE_SELF and MOVE_SELF are about the DIRECTORY: it went away, so
# everything under it did.
#
# CLOSE_WRITE is deliberately absent. It would be one event per save
# where MODIFY is one per write(2), which is fewer calls - and a
# writer using mmap never closes anything, so it would also be zero
# events for a real change. Over-reporting costs a call that forgets
# nothing; under-reporting is silent staleness.
WATCH_MASK = (Mask.MODIFY | Mask.MOVED_TO | Mask.MOVED_FROM
              | Mask.CREATE | Mask.DELETE
              | Mask.DELETE_SELF | Mask.MOVE_SELF)


class Notifier:
    """An inotify source over one `Watcher`.

    Owns the inotify handle, so it is a context manager:

        async with Notifier(watcher) as source:
            await source.eval_file("/path/to/root.nix")
            while True:
                await source.next_change()

    `next_change()` is the whole loop body, and `run()` is that loop.
    The pair exists because a gate needs to advance ONE event and then
    assert - a test driving `run()` could only wait and hope.
    """

    def __init__(self, watcher: Watcher) -> None:
        self._watcher = watcher
        self._inotify = Inotify()
        # directory -> the watch on it. The key is what `sync` diffs,
        # and the value is what `rm_watch` needs.
        self._watches: dict[str, Any] = {}

    # -- lifetime ---------------------------------------------------------
    async def __aenter__(self) -> Notifier:
        await self.sync()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Drop the inotify handle. The watcher is untouched."""
        self._watches.clear()
        self._inotify.close()

    # -- the watch set ----------------------------------------------------
    def directories(self) -> list[str]:
        """The directories currently watched."""
        return sorted(self._watches)

    async def sync(self) -> list[str]:
        """Make the watch set match what the watcher depends on.

        Returns the roots it forgot on the way, which is normally
        empty: a directory that has gone away since it was cached is a
        change, and this is where that is noticed.

        Idempotent, and cheap when nothing moved - it diffs two sets of
        directory names and touches the kernel only for the
        difference. Call it after an evaluation; `eval_file` below
        does.
        """
        wanted = _parents(self._watcher.watching())
        forgotten: list[str] = []

        for gone in sorted(set(self._watches) - wanted):
            # rm_watch on a directory the kernel already dropped -
            # deleted, unmounted - raises, and the watch is gone
            # either way. There is nothing to report and nothing to
            # do.
            with contextlib.suppress(OSError):
                self._inotify.rm_watch(self._watches[gone])
            del self._watches[gone]

        for new in sorted(wanted - set(self._watches)):
            try:
                self._watches[new] = self._inotify.add_watch(
                    new, WATCH_MASK)
            except OSError:
                # The directory is not there. Every file the watcher
                # holds under it is therefore gone, which is a change
                # and not a reason to skip: skipping would leave those
                # roots cached against files that no longer exist,
                # with nothing said.
                for orphan in self._watcher.watching():
                    if os.path.dirname(orphan) == new:
                        forgotten += await self._watcher.changed(orphan)
        return sorted(set(forgotten))

    async def eval_file(self, path: str) -> ValueLike:
        """Evaluate a root through the watcher, then watch its files.

        The convenience, not a third owner of `eval_file`: the answer
        is the watcher's, and the only thing added is the `sync()`
        after it. Calling `Watcher.eval_file` yourself is correct and
        leaves the watch set one `sync()` behind."""
        value = await self._watcher.eval_file(path)
        await self.sync()
        return value

    # -- the loop ---------------------------------------------------------
    async def next_change(self) -> list[str]:
        """Wait for one event that matters, act on it, and answer.

        Returns the roots forgotten. It waits as long as it has to:
        events for files no root depends on are read and dropped here
        rather than passed on, because a watched directory can hold
        thousands of files this evaluation never read.

        Re-syncs afterwards. A forget prunes what the watcher watches,
        so the directories worth listening to change with it.
        """
        while True:
            event = await self._inotify.get()
            paths = self._paths(event)
            if not paths:
                continue
            forgotten: list[str] = []
            for path in paths:
                forgotten += await self._watcher.changed(path)
            forgotten += await self.sync()
            return sorted(set(forgotten))

    async def run(self) -> None:
        """`next_change()` forever. Cancel it to stop."""
        while True:
            await self.next_change()

    # -- events -----------------------------------------------------------
    def _paths(self, event: Any) -> list[str]:
        """Which watched files this event is about. Often none.

        An event names a directory and, usually, a file in it. Two
        shapes, and the second is easy to miss:

        - a NAMED event is about one file, and it matters only if some
          root depends on that file;
        - an UNNAMED event - DELETE_SELF, MOVE_SELF - is about the
          directory itself, so every watched file under it is
          implicated at once.

        `event.path` is None once its watch is gone, which happens for
        the IGNORED that follows a removal. Nothing to report: the
        removal already was.
        """
        watched = self._watcher.watching()
        if event.path is None:
            return []
        path = str(event.path)
        if event.name is None:
            return [p for p in watched if os.path.dirname(p) == path]
        return [p for p in watched if p == path]


def _parents(files: list[str]) -> set[str]:
    """The directories to watch, for a set of files to watch.

    `Path.parent` and not `dirname`, so a bare name answers "." rather
    than the empty string that `add_watch` would refuse."""
    return {str(Path(p).parent) for p in files}
