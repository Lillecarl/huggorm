"""The inotify source: what it watches, and what it wakes for.

`test_watch.py` gates the BOOKKEEPING with no timing at all, because
`changed()` and `rescan()` are explicit steps. This file gates the
adapter over them, and the adapter's whole job is timing - so the
tests here fall into two kinds and neither one sleeps.

Most of them assert on the WATCH SET, which is a pure function of what
the watcher depends on: evaluate a root, then look at which
directories are watched. No event, no wait.

Two of them go end to end, and they wait on the EVENT rather than on
the clock: `next_change()` returns the moment the kernel delivers, and
`anyio.fail_after` is the deadline that turns a missing event into a
named failure instead of a hang. A sleep would be the thing that makes
a suite slow and flaky at once.

Linux only, and unmarked. inotify is a kernel interface, not a
daemon, so a build sandbox has it - which is what separates this from
the `live` marker: those tests need a store this one does not.
"""

from pathlib import Path
from typing import Any

import anyio
import pytest

from huggorm.notify import Notifier
from huggorm.watch import Watcher

URI = "dummy://"

# Long enough that a loaded machine does not lose, short enough that a
# BROKEN adapter fails the suite in seconds rather than hanging it.
# inotify delivers in microseconds when it delivers at all.
DEADLINE = 20.0


@pytest.fixture
def state() -> Any:
    from huggorm_generated import AsyncEvalState

    return AsyncEvalState(URI)


def write(path: Any, text: str) -> str:
    path.write_text(text)
    return str(path)


async def test_it_watches_the_directory_and_not_the_file(
        state: Any, tmp_path: Any) -> None:
    """The one decision this module makes, and it is in the watch set.

    A watch follows the INODE. Most editors save by writing a
    temporary file and renaming it over the original, which gives the
    file a new inode - so a watch on the file survives the save
    pointing at something nobody has any more, and never fires again.
    A watch on the parent sees that rename as MOVED_TO.

    So the assertion is that the directories are watched and the files
    are not, which is what `tasks/083` said to build and what a
    reasonable-looking implementation gets wrong."""
    inner = write(tmp_path / "inner.nix", "40 + 2\n")
    outer = write(tmp_path / "outer.nix", f"import {inner}\n")

    w = Watcher(state)
    async with Notifier(w) as source:
        await source.eval_file(outer)
        assert source.directories() == [str(tmp_path)]
        assert set(w.watching()) == {inner, outer}


async def test_the_watch_set_follows_the_roots(
        state: Any, tmp_path: Any) -> None:
    """A directory is watched because a root depends on something in
    it, and stops being watched when no root does.

    Two directories and two roots, so the second half is a real
    removal rather than a teardown: forgetting `a` drops its
    directory and leaves `b`'s.

    Pure bookkeeping - no event is involved, and nothing here waits."""
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    a = write(one / "a.nix", "1\n")
    b = write(two / "b.nix", "2\n")

    w = Watcher(state)
    async with Notifier(w) as source:
        await source.eval_file(a)
        assert source.directories() == [str(one)]

        await source.eval_file(b)
        assert source.directories() == [str(one), str(two)]

        # `a` is forgotten, so nothing depends on `one` any more.
        # `b`'s snapshot holds `a.nix` as well - a snapshot is a
        # superset of the closure - so it goes too, and this is the
        # over-forgetting `tasks/083` measured rather than a bug.
        await w.changed(a)
        await source.sync()
        assert source.directories() == []


async def test_a_saved_file_wakes_the_watcher(
        state: Any, tmp_path: Any) -> None:
    """End to end: an edit nobody reported forgets the root.

    This is what the module is FOR. `test_watch.py` proves the
    watcher forgets the right thing when told; this proves something
    tells it without being asked.

    It waits on the event, not on the clock. `next_change()` returns
    when the kernel delivers, so the deadline is only there to turn a
    missing event into a named failure."""
    inner = write(tmp_path / "inner.nix", "40 + 2\n")
    outer = write(tmp_path / "outer.nix", f"import {inner}\n")

    w = Watcher(state)
    async with Notifier(w) as source:
        assert await (await source.eval_file(outer)).integer() == 42

        write(tmp_path / "inner.nix", "1 + 1\n")
        with anyio.fail_after(DEADLINE):
            assert await source.next_change() == [outer]

        assert await (await source.eval_file(outer)).integer() == 2


async def test_a_save_by_rename_wakes_it_too(
        state: Any, tmp_path: Any) -> None:
    """The case a file watch cannot see, done the way an editor does.

    Write a temporary file beside the original and rename it over the
    top. The original's inode is gone afterwards, so a watch on the
    file is watching nothing - and a MOVED_TO arrives in the parent,
    which is where this module watches.

    The `.tmp` file is inside the watched directory and no root
    depends on it, so its own CREATE and MODIFY are dropped by the
    filter rather than passed on. That is why this can assert on the
    FIRST change rather than draining a queue."""
    inner = write(tmp_path / "inner.nix", "40 + 2\n")
    outer = write(tmp_path / "outer.nix", f"import {inner}\n")

    w = Watcher(state)
    async with Notifier(w) as source:
        assert await (await source.eval_file(outer)).integer() == 42

        staged = tmp_path / "inner.nix.tmp"
        staged.write_text("1 + 1\n")
        staged.rename(tmp_path / "inner.nix")

        with anyio.fail_after(DEADLINE):
            assert await source.next_change() == [outer]

        assert await (await source.eval_file(outer)).integer() == 2


async def test_the_virtual_entry_is_never_watched(
        state: Any, tmp_path: Any) -> None:
    """libexpr's own file is in the cache and on no filesystem.

    `«nix-internal»/derivation-internal.nix` comes back from
    `cached_files` like any other entry. `Watcher.watching` drops it,
    and this asserts the consequence one layer up: `add_watch` is
    never called with a path built from it.

    THE WATCH SET IS NOT ENOUGH HERE, which is the interesting part.
    A marker reaching `add_watch` raises OSError, and `sync()` turns
    that into a CHANGE rather than a skip - deliberately, and gated
    below. So the watch set comes out identical either way and only
    the ROOT tells the two apart: filtered, it survives; unfiltered,
    the sync that was meant to start watching it forgets it instead,
    on every single evaluation.

    Asserted through the public surface rather than through a mock, so
    it holds whatever the marker turns out to be."""
    root = write(tmp_path / "root.nix", "derivation\n")

    w = Watcher(state)
    async with Notifier(w) as source:
        await source.eval_file(root)
        # Not vacuous: the entry really is in this evaluation's cache.
        assert any(p.startswith("«") for p in await state.cached_files())
        assert source.directories() == [str(tmp_path)]
        # The half that discriminates.
        assert await source.sync() == [], "nothing changed"
        assert w.roots == [root]


async def test_a_deleted_directory_is_a_change(
        state: Any, tmp_path: Any) -> None:
    """A watch that cannot be placed is a CHANGE, not a skip.

    `sync()` runs after an evaluation, and by then the directory a
    root's files live in may be gone. Skipping the failed `add_watch`
    would leave that root cached against files that no longer exist,
    with nothing said - which is this repo's named failure mode
    wearing an OSError.

    So `sync()` reports what it forgot instead, and this is the gate
    on that path."""
    sub = tmp_path / "sub"
    sub.mkdir()
    inner = write(sub / "inner.nix", "40 + 2\n")
    outer = write(tmp_path / "outer.nix", f"import {inner}\n")

    w = Watcher(state)
    source = Notifier(w)
    try:
        assert await (await w.eval_file(outer)).integer() == 42
        # The directory goes away BEFORE anything watched it, so the
        # add is what discovers the loss.
        Path(inner).unlink()
        sub.rmdir()
        assert await source.sync() == [outer]
    finally:
        source.close()


async def test_an_unrelated_file_wakes_nothing(
        state: Any, tmp_path: Any) -> None:
    """The control, and the one a too-eager adapter fails.

    A file in a watched directory that no root depends on raises real
    inotify events. They are dropped here, so `changed()` is never
    called and no root is forgotten.

    Nothing waits for a non-event, which cannot be asserted without a
    timeout. Instead a REAL change follows the noise: if the filter
    let the noise through, the first `next_change()` answers `[]` and
    this fails on the comparison rather than on the clock."""
    inner = write(tmp_path / "inner.nix", "40 + 2\n")
    outer = write(tmp_path / "outer.nix", f"import {inner}\n")

    w = Watcher(state)
    async with Notifier(w) as source:
        assert await (await source.eval_file(outer)).integer() == 42

        write(tmp_path / "notes.txt", "nothing to do with nix\n")
        write(tmp_path / "inner.nix", "1 + 1\n")

        with anyio.fail_after(DEADLINE):
            assert await source.next_change() == [outer]
