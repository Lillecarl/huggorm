"""
Values as msgpack, by their declared `Wire`.

The codec turns a value into what msgpack carries natively, and back.
Every type arrives as a `Wire` the build emitted, so this module reads
no type string and resolves no name.

The shapes:

- An absent value is None. Only an optional or a container may be.
- A scalar is itself. An enum is its str value. A SPELLED scalar is
  its builtin form: a timedelta is whole microseconds.
- A list is a list. A map is a map with str keys.
- A VALUE is the list of its declared parts, in order. An ERROR is
  the same, read off the exception's attributes.
- A UNION is `[arm index, arm]`, the index into its declared arms.
- A PROXY is its handle id.
- A tree node is `[Node, ...]`: see `encode_tree`.

Both ends run the same build, so no shape carries a version or a
field name. msgpack keeps str and bytes apart, which a NAR hash and a
store path name need.

A decode checks each scalar's type before it converts it. A converter
alone accepts too much: `bytes(5)` is five zero bytes.
"""

from __future__ import annotations

import enum
import importlib
from collections.abc import Callable
from types import ModuleType
from typing import Any, assert_never

import msgpack  # type: ignore[import-untyped]

from huggorm_generated._callspec import Arg, Wire, WireKind
from huggorm_generated._policy import (
    ERROR_FIELDS,
    ERROR_MODULE,
    UNION_ARMS,
    WIRE_FIELDS,
)
from huggorm_generated._wiretypes import MAX_UNION_DEPTH, SPELLED

from . import tree

# Each builtin scalar's converter, and the one type msgpack hands back
# for it. `uint` is a width, which is the C++ side's business.
_SCALARS: dict[str, tuple[Callable[[Any], Any], type]] = {
    "str": (str, str), "int": (int, int), "uint": (int, int),
    "float": (float, float), "bool": (bool, bool), "bytes": (bytes, bytes)}

_FLAT = (WireKind.SCALAR, WireKind.ENUM)
_CONTAINER = (WireKind.LIST, WireKind.MAP)


class Node(enum.IntEnum):
    """The first element of an encoded tree node."""

    LEAF = 0
    STAYS = 1
    ITEMS = 2
    ENTRIES = 3


# msgpack carries no annotations. `loads` is its exported name for
# `unpackb`, which it imports under a condition.
_packb: Callable[..., bytes] = msgpack.packb
_unpackb: Callable[..., Any] = msgpack.loads


def pack(obj: Any) -> bytes:
    """An encoded value as bytes. `use_bin_type` keeps bytes apart
    from str."""
    return _packb(obj, use_bin_type=True)


def unpack(data: bytes) -> Any:
    """`pack` reversed. A map key must be a str, as every encoded map
    key is."""
    return _unpackb(data, raw=False, strict_map_key=True, use_list=True)


def _no_proxy(owner: str, part: str) -> Callable[[Any], Any]:
    """The arm a part of a wire value can never take: a wire value is
    rebuilt from its parts on the far side, and a proxy has no object
    there. The build refuses one, so reaching this is a build bug."""
    def refuse(_: Any) -> Any:
        raise TypeError(
            f"{owner}.{part} carries a proxy inside a wire value, which "
            f"the build refuses: a wire value copies all the way down")
    return refuse


def _expect(raw: Any, kind: type, what: str) -> None:
    # bool is an int to isinstance, and an int part must not take one.
    if type(raw) is not kind and not (kind is float and type(raw) is int):
        raise TypeError(
            f"{what} arrived as {type(raw).__name__}, not {kind.__name__}")


class Codec:
    """Encodes and decodes values by their declared `Wire`."""

    def __init__(self, bindings: ModuleType | None = None) -> None:
        self._bindings = bindings
        self.fields: dict[str, tuple[Arg, ...]] = WIRE_FIELDS
        self.errors: dict[str, tuple[Arg, ...]] = ERROR_FIELDS
        self.unions: dict[str, tuple[Wire, ...]] = UNION_ARMS

    @property
    def bindings(self) -> ModuleType:
        if self._bindings is None:
            self._bindings = importlib.import_module("huggorm_bindings")
        return self._bindings

    # -- scalars ----------------------------------------------------------
    def _scalar_out(self, w: Wire, value: Any) -> Any:
        if (spelled := SPELLED.get(w.name)) is not None:
            return spelled.out(value)
        if w.kind is WireKind.ENUM:
            # Through the class, so a word outside the vocabulary
            # raises on the side that typed it.
            return str(getattr(self.bindings, w.name)(value))
        return _SCALARS[w.name][0](value)

    def _scalar_in(self, w: Wire, raw: Any) -> Any:
        if w.kind is WireKind.ENUM:
            _expect(raw, str, w.name)
            return getattr(self.bindings, w.name)(raw)
        if (spelled := SPELLED.get(w.name)) is not None:
            _expect(raw, _SCALARS[spelled.field][1], w.name)
            return spelled.back(raw)
        convert, kind = _SCALARS[w.name]
        _expect(raw, kind, w.name)
        return convert(raw)

    @staticmethod
    def _sync(obj: Any) -> Any:
        """The sync binding object behind a possibly-async wrapper.
        Wire values are pool-threaded, so unwrapping is safe from any
        thread."""
        if getattr(obj, "_runner", None) is None:
            return obj
        from huggorm_generated._runtime import unwrap_arg
        return unwrap_arg(obj)

    # -- values -----------------------------------------------------------
    def encode(self, w: Wire | None, value: Any,
               proxy_id: Callable[[Any], str], depth: int = 0) -> Any:
        """`value` as msgpack-native data. `proxy_id` names a handle
        for a proxy, and is called for nothing else."""
        if w is None or value is None:
            if w is not None and not (w.optional or w.kind in _CONTAINER):
                raise TypeError(f"a {w.name or w.kind} is not optional")
            return None
        match w.kind:
            case WireKind.SCALAR | WireKind.ENUM:
                return self._scalar_out(w, value)
            case WireKind.LIST:
                item = _item(w)
                return [self.encode(item, v, proxy_id, depth) for v in value]
            case WireKind.MAP:
                item = _item(w)
                return {k: self.encode(item, v, proxy_id, depth)
                        for k, v in value.items()}
            case WireKind.VALUE:
                return self._parts_out(w.name, value, depth)
            case WireKind.UNION:
                return self._union_out(w.name, value, depth)
            case WireKind.ERROR:
                return [self.encode(f.type, getattr(value, f.name),
                                    _no_proxy(w.name, f.name))
                        for f in self.errors[w.name]]
            case WireKind.PROXY:
                return proxy_id(value)
            case _:
                assert_never(w.kind)

    def decode(self, w: Wire | None, raw: Any,
               proxy_obj: Callable[[str], Any], depth: int = 0) -> Any:
        """`encode` reversed. `proxy_obj` turns a handle id into the
        object it names, and is called for nothing else.

        An absent container reads back as an empty one unless the
        declaration says it may be None."""
        if w is None:
            return None
        if raw is None:
            if w.optional:
                return None
            match w.kind:
                case WireKind.LIST:
                    return []
                case WireKind.MAP:
                    return {}
            raise TypeError(f"a {w.name or w.kind} arrived absent, and it "
                            f"is not optional")
        match w.kind:
            case WireKind.SCALAR | WireKind.ENUM:
                return self._scalar_in(w, raw)
            case WireKind.LIST:
                item = _item(w)
                _expect(raw, list, "a list")
                return [self.decode(item, v, proxy_obj, depth) for v in raw]
            case WireKind.MAP:
                item = _item(w)
                _expect(raw, dict, "a map")
                return {k: self.decode(item, v, proxy_obj, depth)
                        for k, v in raw.items()}
            case WireKind.VALUE:
                return getattr(self.bindings, w.name)._from_parts(
                    *self._parts_in(w.name, self.fields[w.name], raw, depth))
            case WireKind.UNION:
                return self._union_in(w.name, raw, depth)
            case WireKind.ERROR:
                kls = getattr(importlib.import_module(ERROR_MODULE), w.name)
                return kls(*self._parts_in(w.name, self.errors[w.name], raw,
                                           depth))
            case WireKind.PROXY:
                _expect(raw, str, "a handle id")
                return proxy_obj(raw)
            case _:
                assert_never(w.kind)

    def _parts_out(self, cls: str, obj: Any, depth: int) -> list[Any]:
        parts = self._sync(obj)._parts()
        declared = self.fields[cls]
        if len(parts) != len(declared):
            raise TypeError(
                f"{cls}._parts() returned {len(parts)} value(s) for "
                f"{len(declared)} declared _wire_fields")
        return [self.encode(f.type, v, _no_proxy(cls, f.name), depth)
                for f, v in zip(declared, parts, strict=True)]

    def _parts_in(self, cls: str, declared: tuple[Arg, ...], raw: Any,
                  depth: int) -> list[Any]:
        _expect(raw, list, cls)
        if len(raw) != len(declared):
            raise TypeError(
                f"a {cls} arrived with {len(raw)} part(s), not "
                f"{len(declared)}")
        return [self.decode(f.type, v, _no_proxy(cls, f.name), depth)
                for f, v in zip(declared, raw, strict=True)]

    # -- unions -----------------------------------------------------------
    def _union_out(self, alias: str, obj: Any, depth: int) -> list[Any]:
        """By isinstance over the declared arms, in order. A scalar arm
        matches its EXACT type: `True` is an `int` to isinstance."""
        _not_too_deep(alias, depth)
        held = self._sync(obj)
        for i, arm in enumerate(self.unions[alias]):
            if arm.kind in _FLAT:
                exact = (getattr(self.bindings, arm.name)
                         if arm.kind is WireKind.ENUM
                         else _SCALARS[arm.name][1])
                if type(held) is exact:
                    return [i, self._scalar_out(arm, held)]
            elif isinstance(held, getattr(self.bindings, arm.name)):
                return [i, self._parts_out(arm.name, held, depth + 1)]
        raise TypeError(
            f"{type(obj).__name__} is not one of {alias}'s arms "
            f"({', '.join(a.name for a in self.unions[alias])})")

    def _union_in(self, alias: str, raw: Any, depth: int) -> Any:
        _not_too_deep(alias, depth)
        arms = self.unions[alias]
        if (type(raw) is not list or len(raw) != 2 or type(raw[0]) is not int
                or not 0 <= raw[0] < len(arms)):
            raise TypeError(f"a {alias} arrived as {raw!r}, not "
                            f"[arm index, arm]")
        arm = arms[raw[0]]
        if arm.kind in _FLAT:
            return self._scalar_in(arm, raw[1])
        return self.decode(arm, raw[1], _no_proxy(alias, arm.name),
                           depth + 1)

    # -- value trees ------------------------------------------------------
    def encode_tree(self, node: tree.Node,
                    proxy_id: Callable[[str, Any], str]) -> list[Any]:
        """One walked node. A leaf is msgpack-native already: the walk
        reads only str, int, bool and float."""
        match node:
            case tree.Leaf(wire=wire, value=value):
                return [Node.LEAF, _SCALARS[wire][0](value)]
            case tree.Stays(cls=cls, obj=obj):
                # A handle does not say what it is, and no layer above
                # the bindings may name a class. The walk knows.
                return [Node.STAYS, cls, proxy_id(cls, obj)]
            case tree.Items(cls=cls, obj=obj, items=items):
                return [Node.ITEMS, cls, proxy_id(cls, obj),
                        [self.encode_tree(i, proxy_id) for i in items]]
            case tree.Entries(cls=cls, obj=obj, entries=entries):
                return [Node.ENTRIES, cls, proxy_id(cls, obj),
                        {k: self.encode_tree(v, proxy_id)
                         for k, v in entries.items()}]
            case _:
                assert_never(node)

    def decode_tree(self, raw: Any,
                    proxy_obj: Callable[[str, str], Any],
                    holder: Callable[[str, str, Any], Any]) -> Any:
        """A Python value from an encoded tree. `holder` builds a list
        or an attribute set around its handle and its decoded
        contents. An attribute set comes back sorted by name, which is
        the order Nix lists one in."""
        _expect(raw, list, "a tree node")
        match Node(raw[0]), raw[1:]:
            case Node.LEAF, [value]:
                return value
            case Node.STAYS, [str(cls), str(hid)]:
                return proxy_obj(cls, hid)
            case Node.ITEMS, [str(cls), str(hid), list(items)]:
                return holder(cls, hid, [self.decode_tree(i, proxy_obj, holder)
                                         for i in items])
            case Node.ENTRIES, [str(cls), str(hid), dict(entries)]:
                return holder(cls, hid, {
                    k: self.decode_tree(entries[k], proxy_obj, holder)
                    for k in sorted(entries)})
        raise TypeError(f"a tree node arrived as {raw!r}")


def _item(w: Wire) -> Wire:
    if w.item is None:
        raise TypeError(f"a {w.kind} Wire with no item")
    return w.item


def _not_too_deep(alias: str, depth: int) -> None:
    """A union arm may hold the union again, so a peer could send a
    chain long enough to reach Python's recursion limit. Real chains
    are one or two deep."""
    if depth > MAX_UNION_DEPTH:
        raise ValueError(
            f"a {alias} nested more than {MAX_UNION_DEPTH} deep")
