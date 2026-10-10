"""
Read-only views of a realized value tree (huggorm#147).

A list or an attribute set that `realize` walked comes back as one of
these. Each IS the async class of its node, on a remote backend, so
every remote method still works and the view passes wherever that
class does: a Nix
function called with a view gets the value behind its handle. Reads
of what the walk carried are local and synchronous.

They are read-only because the value is: a Nix value never changes,
so the contents never go stale. To change one, copy it - `dict(view)`
or `view | {...}` gives a plain dict, and a plain value crosses as
data.

Mixins, joined to the async class at run time, because this layer
names no binding type. The client sets `_contents`.
"""

from __future__ import annotations

import types
from collections.abc import Iterator, Mapping
from typing import Any


class AttrsView:
    """An attribute set: `view[name]`, `len`, `in`, iteration in name
    order, `keys`, `values`, `items`.

    Not a `Mapping`: `Mapping.get` would shadow the remote `get`,
    which reads an attribute through its handle."""

    _contents: dict[str, Any]

    def __getitem__(self, name: str) -> Any:
        return self._contents[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._contents)

    def __len__(self) -> int:
        return len(self._contents)

    def __contains__(self, name: object) -> bool:
        return name in self._contents

    def keys(self) -> Any:
        return self._contents.keys()

    def values(self) -> Any:
        return self._contents.values()

    def items(self) -> Any:
        return self._contents.items()

    def __or__(self, other: Mapping[str, Any]) -> dict[str, Any]:
        return {**self._contents, **other}

    def __ror__(self, other: Mapping[str, Any]) -> dict[str, Any]:
        return {**other, **self._contents}

    def __eq__(self, other: object) -> bool:
        if isinstance(other, AttrsView):
            return self._contents == other._contents
        if isinstance(other, Mapping):
            return self._contents == dict(other)
        return NotImplemented

    __hash__ = object.__hash__

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._contents!r}>"


class ListView:
    """A list: `view[i]`, slices, `len`, iteration."""

    _contents: list[Any]

    def __getitem__(self, index: Any) -> Any:
        return self._contents[index]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._contents)

    def __len__(self) -> int:
        return len(self._contents)

    def __contains__(self, item: object) -> bool:
        return item in self._contents

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ListView):
            return self._contents == other._contents
        if isinstance(other, list | tuple):
            return self._contents == list(other)
        return NotImplemented

    __hash__ = object.__hash__

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._contents!r}>"


_JOINED: dict[tuple[type, type], type] = {}


def view_class(mixin: type, base: type) -> type:
    """The async class `base` with `mixin`'s reads, built once."""
    key = (mixin, base)
    if key not in _JOINED:
        name = base.__name__ + mixin.__name__.removesuffix("View")
        _JOINED[key] = types.new_class(name, (mixin, base))
    return _JOINED[key]
