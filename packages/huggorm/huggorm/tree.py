"""
The nodes of a walked value tree.

The server's walk builds these on the value's own thread, and a codec
writes them out. A node's class is its kind, so no reader matches on a
tag string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Leaf:
    """A forced scalar. `wire` is its declared type."""

    wire: str
    value: Any


@dataclass(frozen=True, slots=True)
class Stays:
    """A node that stays remote: a thunk, a repeat, or one past the
    walk's bounds. `cls` names it, because a handle does not."""

    cls: str
    obj: Any


@dataclass(frozen=True, slots=True)
class Items:
    items: list[Node]


@dataclass(frozen=True, slots=True)
class Entries:
    entries: dict[str, Node]


Node = Leaf | Stays | Items | Entries
