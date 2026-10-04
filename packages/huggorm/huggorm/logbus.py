"""One reader's share of a log subscription many readers read.

The binding REPLACES a thread's subscription, so a second
`subscribe_logs` on one state silences the first reader (huggorm#85).
Both the server and the local session therefore open one subscription
per state and fan it out. This module holds the part both fan-outs
agree on: what a reader keeps, what it drops, and at which level the
shared subscription opens.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# What a reader gets when the caller names neither. The binding's own
# defaults, restated because the shared subscription does not pass a
# reader's through (`decl/eval.py`, `EvalState.subscribe_logs`).
LOG_CAPACITY = 1024
LOG_LEVEL = 3


def widest(readers: Iterable[Share]) -> int:
    """The level the shared subscription opens at.

    The WIDEST any reader asked for, and not a constant 7: a
    subscription raises the level `RemoteStore::setOptions` sends to
    the daemon, so subscribing at 7 asks every daemon connection to
    narrate at vomit whether or not anyone wants it (huggorm#102). A
    reader that wants more REOPENS the subscription at its level
    rather than being refused."""
    return max((r.level for r in readers), default=LOG_LEVEL)


class Share:
    """One reader's buffer, under the shared queue's bound.

    The DROP POLICY is the C++ queue's, restated because a reader is a
    second bound under the first. A full reader refuses a "msg" and a
    "result" and nothing else, for the reason `LogQueue` gives: a
    dropped stop leaks a node in the reader's activity tree that
    nothing later closes.

    The test names what to DROP, and that is what made `"finalized"`
    safe to add without touching this class. Had it listed what to
    KEEP, a new control event would be lost here in silence.

    `level` filters a "msg" only, for the same reason.
    """

    def __init__(self, capacity: int, level: int) -> None:
        self._capacity = capacity
        # PUBLIC, because the fan-out reads it to compute `widest`.
        self.level = level
        self._records: list[Any] = []
        self.own_dropped = 0

    def offer(self, record: Any) -> None:
        """One record from the shared drain. Never awaits."""
        action = record.action()
        if action == "msg" and record.level() > self.level:
            return
        if action in ("msg", "result") and len(self._records) >= self._capacity:
            self.own_dropped += 1
            return
        self._records.append(record)

    def take(self) -> list[Any]:
        """Everything this reader holds, and it holds nothing after."""
        out, self._records = self._records, []
        return out
