"""The eager pass: what it re-evaluates, and what it refuses to do.

Split the way the module is. Most of these drive `stale()` and
`refresh()`, which have no timer, no thread and no wait - so they read
like `test_watch.py` and nothing here sleeps.

Two drive `follow()`, which is the one thing here that needs a clock.
They wait on the OUTCOME under a deadline rather than on the clock
itself, so a working implementation finishes them in milliseconds and
a broken one names itself.

One of those two holds a NEGATIVE - that a second evaluation does not
happen - and that is the only sleep in this file. There is no way to
assert that something did not happen except by waiting past the point
where it would have. It is also the gate that caught a measurement
this module had already believed; the docstring says how.

`stale()` is derived rather than recorded, and that is the fact most
of this file is about. `Watcher.changed()` deletes a forgotten root's
snapshot, so "registered and has no snapshot" already means "its
answer is gone" - and a warmer that kept its own staleness flag could
disagree with the watcher. It has none to disagree with.
"""

from typing import Any

import anyio
import pytest

from huggorm.notify import Notifier
from huggorm.warm import SETTLE, Warmer
from huggorm.watch import Watcher

URI = "dummy://"

# The end-to-end gates return as soon as the work is done, so this
# only bounds a failure.
DEADLINE = 20.0


@pytest.fixture
def state() -> Any:
    from huggorm_generated import AsyncEvalState

    return AsyncEvalState(URI)


def write(path: Any, text: str) -> str:
    path.write_text(text)
    return str(path)


async def until(check: Any, deadline: float = DEADLINE) -> None:
    """Wait for a condition the background task will make true.

    A poll and not a sleep: it returns on the first pass that holds,
    so a working implementation costs one scheduler turn. The deadline
    is what turns "never" into a named failure instead of a hang.
    """
    with anyio.fail_after(deadline):
        while not check():
            await anyio.sleep(0)


async def test_a_registered_root_is_stale_until_it_is_evaluated(
        state: Any, tmp_path: Any) -> None:
    """Registering evaluates nothing, and says so.

    A root with no snapshot is stale whether it was forgotten or was
    never evaluated at all - the two are the same state and this
    module does not distinguish them, because the watcher does not
    either."""
    root = write(tmp_path / "root.nix", "1 + 1\n")

    w = Watcher(state)
    warm = Warmer(w)
    warm.register(root)

    assert warm.registered == [root]
    assert warm.stale() == [root], "registering does not evaluate"

    assert await warm.refresh() == [root]
    assert warm.stale() == [], "and now it is warm"


async def test_a_change_makes_a_registered_root_stale_again(
        state: Any, tmp_path: Any) -> None:
    """The whole mechanism, with the noticing taken out.

    `changed()` forgets the root, which drops its snapshot, which is
    what `stale()` reads. Nothing here records staleness, so there is
    nothing for the two to disagree about."""
    inner = write(tmp_path / "inner.nix", "40 + 2\n")
    outer = write(tmp_path / "outer.nix", f"import {inner}\n")

    w = Watcher(state)
    warm = Warmer(w)
    warm.register(outer)
    await warm.refresh()
    assert warm.stale() == []

    write(tmp_path / "inner.nix", "1 + 1\n")
    assert await w.changed(inner) == [outer]
    assert warm.stale() == [outer]

    assert await warm.refresh() == [outer]
    assert await (await w.eval_file(outer)).integer() == 2


async def test_an_unregistered_root_is_never_refreshed(
        state: Any, tmp_path: Any) -> None:
    """Registration is what this class adds, so it has to bite.

    A watcher records every root anyone evaluated through it.
    Re-evaluating all of them would make every session's whole history
    a background job, so only the registered ones come back."""
    inner = write(tmp_path / "inner.nix", "40 + 2\n")
    kept = write(tmp_path / "kept.nix", f"import {inner}\n")
    cold = write(tmp_path / "cold.nix", f"import {inner}\n")

    w = Watcher(state)
    warm = Warmer(w)
    warm.register(kept)
    await w.eval_file(kept)
    await w.eval_file(cold)

    write(tmp_path / "inner.nix", "1 + 1\n")
    assert sorted(await w.changed(inner)) == [cold, kept]

    # Both were forgotten; only the registered one comes back.
    assert await warm.refresh() == [kept]
    assert w.roots == [kept]


async def test_a_root_that_does_not_parse_is_recorded_not_raised(
        state: Any, tmp_path: Any) -> None:
    """A file saved mid-edit is the normal case, not an error.

    An eager pass has no caller to raise to, so raising would take
    down whatever loop is driving it for something a human is about to
    fix by finishing the line. It records the failure and leaves the
    root stale.

    STALE is the important half. A failure that marked the root warm
    would leave it never re-evaluated, and the caller asking later
    would get an answer built from a file that no longer exists in
    that form."""
    root = write(tmp_path / "root.nix", "1 + 1\n")

    w = Watcher(state)
    warm = Warmer(w)
    warm.register(root)
    assert await warm.refresh() == [root]

    write(tmp_path / "root.nix", "1 + \n")
    await w.changed(root)

    assert await warm.refresh() == [], "nothing worked"
    assert root in warm.failures
    assert warm.stale() == [root], "so the next change tries again"

    # And it does, once the file parses.
    write(tmp_path / "root.nix", "2 + 2\n")
    await w.changed(root)
    assert await warm.refresh() == [root]
    assert warm.failures == {}, "a success clears the failure"


async def test_unregistering_stops_the_pass_and_drops_the_failure(
        state: Any, tmp_path: Any) -> None:
    """A failure is about the next attempt, and there will not be one."""
    root = write(tmp_path / "root.nix", "1 + \n")

    w = Watcher(state)
    warm = Warmer(w)
    warm.register(root)
    await warm.refresh()
    assert root in warm.failures

    warm.unregister(root)
    assert warm.registered == []
    assert warm.stale() == []
    assert warm.failures == {}


async def test_a_saved_file_is_re_evaluated_with_nobody_asking(
        state: Any, tmp_path: Any) -> None:
    """The destination, in one test.

    Nobody calls anything after the save. An inotify event reaches the
    notifier, the watcher forgets the root, the warmer re-evaluates
    it, and the answer is the new one before any caller asks.

    It waits on the OUTCOME rather than on the clock: `until` returns
    on the first pass that holds, so this costs a scheduler turn when
    it works and names itself when it does not.

    It waits for the EVALUATION, not for `stale()` to be empty. That
    was the first shape and it was vacuous: a warm root is already
    unstale, so the wait returned before the save was ever noticed and
    the test asserted on the old answer."""
    inner = write(tmp_path / "inner.nix", "40 + 2\n")
    outer = write(tmp_path / "outer.nix", f"import {inner}\n")

    w = Watcher(state)
    warm = Warmer(w)
    warm.register(outer)

    evaluated: list[str] = []
    real = w.eval_file

    async def counted(path: str) -> Any:
        evaluated.append(path)
        return await real(path)

    w.eval_file = counted  # type: ignore[method-assign]

    async with Notifier(w) as source, anyio.create_task_group() as tg:
        await warm.refresh()
        assert await (await w.eval_file(outer)).integer() == 42
        await source.sync()
        evaluated.clear()

        tg.start_soon(warm.follow, source, SETTLE)
        write(tmp_path / "inner.nix", "1 + 1\n")

        # Nobody called anything. The eager pass did.
        await until(lambda: evaluated == [outer])
        assert await (await w.eval_file(outer)).integer() == 2
        tg.cancel_scope.cancel()


async def test_one_save_is_one_evaluation(
        state: Any, tmp_path: Any) -> None:
    """One save is one evaluation, and the settle window is why.

    This gate earned its place by catching a measurement that was
    wrong. A drain loop reading changes across a save saw exactly ONE,
    which said the window was pointless: the first event forgets the
    root, which empties `Watcher.watching()`, so later events of the
    burst match nothing.

    The probe never REFRESHED between events. `follow()` does, and a
    refresh re-evaluates the root and re-watches its files - which is
    what lets the straggler through. Take the drain out of `follow()`
    and this fails with two evaluations for one save. The window is
    measured now rather than argued.

    The `sleep` is the price of asserting a NEGATIVE: a second
    evaluation not happening cannot be waited for, only waited past.

    Counted through the watcher rather than through a spy: `eval_file`
    is what an evaluation costs."""
    inner = write(tmp_path / "inner.nix", "40 + 2\n")
    outer = write(tmp_path / "outer.nix", f"import {inner}\n")

    w = Watcher(state)
    warm = Warmer(w)
    warm.register(outer)

    evaluated: list[str] = []
    real = w.eval_file

    async def counted(path: str) -> Any:
        evaluated.append(path)
        return await real(path)

    w.eval_file = counted  # type: ignore[method-assign]

    async with Notifier(w) as source, anyio.create_task_group() as tg:
        await warm.refresh()
        await source.sync()
        evaluated.clear()

        tg.start_soon(warm.follow, source, SETTLE)
        write(tmp_path / "inner.nix", "1 + 1\n")

        await until(lambda: evaluated == [outer])
        # Long enough that a straggler from the same save would have
        # arrived. Nothing here is waiting FOR it - the assertion is
        # that it does not come.
        await anyio.sleep(0.3)
        assert evaluated == [outer], "one save, one evaluation"
        tg.cancel_scope.cancel()
