"""The watcher: what it forgets, and what it must not miss.

Driven by explicit steps rather than by events. `changed()` says a
path moved and `rescan()` stats for itself, so nothing here sleeps,
polls or waits on a timer - the same reason every gate in `tasks/016`
avoided counters.

In-process only. The watcher is written against `EvalStateLike`, and
`test_parity` is where a claim about all three surfaces belongs; what
these test is the BOOKKEEPING, which is the same object either way.
"""

from typing import Any

import pytest

from huggorm.watch import Watcher

URI = "dummy://"


@pytest.fixture
def state() -> Any:
    from huggorm_generated import AsyncEvalState

    return AsyncEvalState(URI)


def write(path: Any, text: str) -> str:
    path.write_text(text)
    return str(path)


async def test_a_root_is_forgotten_when_its_import_changes(
        state: Any, tmp_path: Any) -> None:
    """The base case, and the one `tasks/016` already gated.

    Here it runs through the watcher instead of by hand, so what is
    tested is that the bookkeeping picks the right files - not that
    `forget_file` erases them."""
    inner = write(tmp_path / "inner.nix", "40 + 2\n")
    outer = write(tmp_path / "outer.nix", f"import {inner}\n")

    w = Watcher(state)
    assert await (await w.eval_file(outer)).integer() == 42

    write(tmp_path / "inner.nix", "1 + 1\n")
    assert await w.changed(inner) == [outer]

    assert await (await w.eval_file(outer)).integer() == 2


async def test_a_shared_import_forgets_both_roots(
        state: Any, tmp_path: Any) -> None:
    """The case the DIFF model got wrong, which is why this exists.

    `tasks/083` measured it: the second root to import a shared file
    gets a `cached_files` diff that does not mention it, because the
    first root already cached it. A watcher built on diffs answers 42
    here - silently, which is this repo's named failure mode.

    Both roots, and the order matters: `b` is the one the diff model
    misses, so a regression that reverted the snapshot policy would
    fail on `b` and pass on `a`."""
    shared = write(tmp_path / "shared.nix", "40 + 2\n")
    a = write(tmp_path / "a.nix", f"import {shared}\n")
    b = write(tmp_path / "b.nix", f"import {shared}\n")

    w = Watcher(state)
    assert await (await w.eval_file(a)).integer() == 42
    assert await (await w.eval_file(b)).integer() == 42

    write(tmp_path / "shared.nix", "1 + 1\n")
    assert await w.changed(shared) == [a, b]

    assert await (await w.eval_file(a)).integer() == 2
    assert await (await w.eval_file(b)).integer() == 2, "the diff model's bug"


async def test_rescan_finds_the_change_without_being_told(
        state: Any, tmp_path: Any) -> None:
    """The dependency-free change source, end to end.

    `changed()` is told; `rescan()` stats. A file written twice inside
    one filesystem tick is why the stamp carries the size as well as
    the mtime - and a test does exactly that."""
    inner = write(tmp_path / "inner.nix", "40 + 2\n")
    outer = write(tmp_path / "outer.nix", f"import {inner}\n")

    w = Watcher(state)
    assert await (await w.eval_file(outer)).integer() == 42

    # Nothing moved, so nothing is forgotten. The control: a rescan
    # that forgot on every call would pass the assertion below and
    # fail here.
    assert await w.rescan() == []

    write(tmp_path / "inner.nix", "1 + 1\n")
    assert await w.rescan() == [outer]
    assert await (await w.eval_file(outer)).integer() == 2


async def test_a_deleted_dependency_counts_as_a_change(
        state: Any, tmp_path: Any) -> None:
    """Absence is an answer too.

    The evaluation that cached the file READ it, so its removal makes
    the cached answer wrong in the same way an edit does. The re-eval
    then fails, which is the correct outcome and not this module's to
    translate."""
    inner = write(tmp_path / "inner.nix", "40 + 2\n")
    outer = write(tmp_path / "outer.nix", f"import {inner}\n")

    w = Watcher(state)
    assert await (await w.eval_file(outer)).integer() == 42

    (tmp_path / "inner.nix").unlink()
    assert await w.rescan() == [outer]

    with pytest.raises(Exception) as caught:
        await w.eval_file(outer)
    assert "inner.nix" in str(caught.value)


async def test_the_virtual_cache_entry_is_never_watched(
        state: Any, tmp_path: Any) -> None:
    """libexpr caches something that is not a file.

    `«nix-internal»/derivation-internal.nix` comes out of an in-memory
    accessor. A watcher that handed it to `os.stat` would see it
    vanish on every rescan and forget everything, forever."""
    src = write(tmp_path / "answer.nix", "40 + 2\n")

    w = Watcher(state)
    await w.eval_file(src)

    assert src in w.watching()
    assert not [p for p in w.watching() if p.startswith("«")], w.watching()
    # The proof it was not merely absent from the cache: it IS there.
    assert any(p.startswith("«") for p in await state.cached_files())

    assert await w.rescan() == []


async def test_an_unrelated_path_forgets_nothing(
        state: Any, tmp_path: Any) -> None:
    """A change source may over-report, and it costs one call.

    Without this, a watcher that forgot everything on every event
    would pass every test above."""
    src = write(tmp_path / "answer.nix", "40 + 2\n")

    w = Watcher(state)
    await w.eval_file(src)

    assert await w.changed(str(tmp_path / "not-a-dependency.nix")) == []
    assert w.roots == [src]


async def test_forgetting_a_root_stops_watching_its_files(
        state: Any, tmp_path: Any) -> None:
    """A watcher meant to run for days cannot grow without bound.

    Forgetting a root drops its snapshot, and the files only that
    root depended on are then watched by nothing. Without the prune
    they would be stat-ed on every rescan for the life of the state.

    Asserted on a PRIVATE, which is unusual here and deliberate. The
    prune changes what the watcher SPENDS, not what it answers: with
    it removed every assertion below still passes, because a stamp for
    a file no root depends on can never implicate a root. So there is
    nothing in the public surface to hold it to, and the choice is
    between reaching in and not gating it at all."""
    inner = write(tmp_path / "inner.nix", "40 + 2\n")
    outer = write(tmp_path / "outer.nix", f"import {inner}\n")

    w = Watcher(state)
    await w.eval_file(outer)
    assert inner in w.watching()

    (tmp_path / "inner.nix").unlink()
    assert await w.rescan() == [outer]

    # The root is forgotten, so nothing depends on either file now.
    assert w.roots == []
    assert w.watching() == []
    assert await w.rescan() == []

    # The part the public surface cannot see. Seen to fail with the
    # prune removed: 2 entries survive, one of them a file that no
    # longer exists.
    assert w._seen == {}, "a watcher for days cannot keep stamps forever"
