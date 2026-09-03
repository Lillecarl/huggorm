"""
Evaluating before anyone asks.

The last third of `tasks/016` and the last piece of the destination in
`CLAUDE.md`: registered expressions evaluated eagerly in the
background, so a user-triggered evaluation is already in progress or
already done.

It needed `tasks/083` first, and only that. Re-evaluating eagerly is
useful once something knows the old answer is stale, and the watcher
knows: `Watcher.changed()` deletes a forgotten root's snapshot, so a
registered root with no snapshot is a root whose answer is gone. This
module adds no bookkeeping of its own to work that out.

## Three layers, and each one is the previous one's consumer

    Watcher      decides what to forget when a path changes
    Notifier     notices a path changed, without being asked
    Warmer       re-evaluates the registered roots that were forgotten

The split that matters here is the same one `tasks/083` made, one
level up.

- **What to refresh** is `stale()`, and **refreshing** is `refresh()`.
  Neither has a timer, a thread or a wait, so a gate drives both with
  no sleeps at all.
- **WHEN to refresh** is `follow()`, and it is the only thing in this
  area that needs a clock.

## Why `follow()` needs a debounce, which was nearly removed

One save is several inotify events. Each of them forgets the root, so
a refresh per event evaluates the same root twice - and an evaluation
is the expensive thing this module exists to do fewer of. So the
first change starts a `settle` window, everything arriving inside it
is drained, and the refresh happens once at the end.

That was argued rather than measured, so it was measured - and the
measurement said the window was pointless. A drain loop reading
changes across a save saw exactly ONE, because the first event forgets
the root, which empties `Watcher.watching()`, so later events of the
burst match nothing.

The measurement was wrong, and `test_one_save_is_one_evaluation` is
what caught it. The probe never REFRESHED between events; `follow()`
does, and a refresh re-evaluates the root and re-watches its files -
which is exactly what lets the straggler through. Take the drain out
and that gate fails with two evaluations for one save.

So the window stays, and it is measured now rather than assumed.
`tasks/086` records both the wrong measurement and the gate that
refuted it.

## What it does not do

**It does not make a user's call faster while it runs.** An
`EvalState` is affine: one thread, one evaluation at a time. An eager
evaluation therefore holds the watcher's lock, and a user calling
`Watcher.eval_file` waits for it. `refresh()` takes the lock once PER
ROOT rather than once for the batch, so the wait is one evaluation and
not the whole refresh - but it is not zero, and no arrangement of this
module makes it zero. The fix would be a second `EvalState` for the
eager pass, which is a different design and a different task.

**It does not cancel an evaluation in flight.** libexpr offers no way
to. Cancelling here could only mean "stopped waiting for it", and the
evaluation would keep running on that thread - so nothing here
pretends to.

**It does not retry on its own.** A file saved mid-edit does not
parse, and the eager evaluation raises. The failure is RECORDED
against the root and not raised, because there is nobody to raise it
to, and the root stays stale - so the next change refreshes it again.
That is one attempt per change rather than a loop, and it is why
`refresh()` is edge-driven rather than something to call in a loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import anyio

if TYPE_CHECKING:
    from .watch import Watcher


# How long to keep draining changes before refreshing. One save is
# several events, and a refresh between two of them re-watches the
# files - so without this the straggler forgets the root again and it
# is evaluated twice. Short enough that a human saving a file does not
# notice it.
#
# A guess about editors rather than a measurement, and the only number
# in this area that is. `follow(settle=...)` is how a caller disagrees.
SETTLE = 0.05


class ChangeSource(Protocol):
    """Anything that can say "something changed" and wait to say it.

    `Notifier` is the one that exists. Declared as a protocol rather
    than imported, because this module has no opinion about where a
    change comes from - the same separation `tasks/083` made one layer
    down, so a second source is a second implementer and not an edit
    here.
    """

    async def next_change(self) -> list[str]:
        ...


class Warmer:
    """The registered roots, and the eager pass that keeps them warm.

    Registration is what separates this from the watcher below it. A
    watcher records every root anyone evaluated through it; a warmer
    re-evaluates only the ones somebody asked to keep warm, because
    evaluating everything a session ever touched is not a service, it
    is a busy loop.
    """

    def __init__(self, watcher: Watcher) -> None:
        self._watcher = watcher
        self._registered: set[str] = set()
        # root -> the failure its last eager evaluation raised. A
        # root that succeeded is absent, so this is the answer to
        # "did the last eager pass work", not a log.
        self._failures: dict[str, Exception] = {}

    # -- the registry -----------------------------------------------------
    def register(self, root: str) -> None:
        """Keep this root warm. Evaluates nothing by itself.

        Registering an already-evaluated root leaves it alone: it has
        a snapshot, so it is not stale, so the next refresh skips it.
        """
        self._registered.add(root)

    def unregister(self, root: str) -> None:
        """Stop keeping it warm. The watcher still watches its files.

        Forgetting the failure with it, because a failure is about the
        last eager attempt and there will not be another.
        """
        self._registered.discard(root)
        self._failures.pop(root, None)

    @property
    def registered(self) -> list[str]:
        return sorted(self._registered)

    @property
    def failures(self) -> dict[str, Exception]:
        """The roots whose last eager evaluation raised, and what it
        raised. A root that then succeeds leaves this."""
        return dict(self._failures)

    # -- the pass ---------------------------------------------------------
    def stale(self) -> list[str]:
        """The registered roots whose answer the watcher has dropped.

        DERIVED, and that is the point: `Watcher.changed()` deletes a
        forgotten root's snapshot, so "registered and has no snapshot"
        already means "was forgotten, or was never evaluated". This
        module records nothing to know it, so the two cannot disagree.
        """
        return sorted(self._registered - set(self._watcher.roots))

    async def refresh(self) -> list[str]:
        """Evaluate every stale registered root. Answers the ones that
        worked.

        One root at a time, and the lock is the watcher's - so a user
        calling `Watcher.eval_file` waits for at most ONE eager
        evaluation rather than for the whole batch.

        A root that raises is recorded in `failures` and left stale.
        Nothing is raised out of here: an eager pass has no caller to
        tell, and a half-saved file that does not parse is the normal
        case rather than an error. It stays stale, so the next change
        tries it again - which is one attempt per change, not a retry
        loop.
        """
        done: list[str] = []
        for root in self.stale():
            try:
                await self._watcher.eval_file(root)
            except Exception as e:
                self._failures[root] = e
            else:
                self._failures.pop(root, None)
                done.append(root)
        return done

    # -- when ------------------------------------------------------------
    async def follow(self, source: ChangeSource,
                     settle: float = SETTLE) -> None:
        """Refresh whenever the source says something changed. Forever.

        The only part of this module that waits on a clock. One save
        is several events, and refreshing between two of them
        re-watches the files - so the straggler forgets the root again
        and it is evaluated twice. The window drains the burst first.

        It costs its full `settle` every time, because there is no way
        to learn that nothing more is coming except by waiting for it.
        Paid once per burst rather than once per event.

        Cancel it to stop. It holds nothing that needs closing.
        """
        while True:
            await source.next_change()
            with anyio.move_on_after(settle):
                while True:
                    await source.next_change()
            await self.refresh()
