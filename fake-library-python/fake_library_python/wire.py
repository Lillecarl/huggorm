"""
Manifest-driven wire codec, shared by the server and the client.

This module knows the SHAPE of the problem - scalars go in fields,
wire-values decompose into their declared parts, proxies travel as
handles - and nothing about the types. Every type name it acts on comes
out of the manifest, which got it from a `_wire` / `_wire_fields`
declaration next to the binding itself.

That is the point. Before this existed, the four layers above the
bindings each carried their own copy of the sentence "StorePath and
DerivedPath serialize, Value and Derivation do not": the schema builder,
the server's decode, the server's encode and the client's invoke.
Adding a type meant editing all four, and forgetting one produced a
KeyError on the first call that touched it.

The two sides differ in exactly one respect, so it is the one thing
they pass in: what a proxy handle means. The server resolves an id to a
live wrapper and registers new ones; the client turns an id into a
RemoteObj and reads ids back off one.
"""

import importlib
from collections.abc import Callable
from types import ModuleType
from typing import Any, ClassVar

from fake_library_generated._wiretypes import SCALAR_NAMES, map_value

# str/int/bool as a lookup. Annotated because the inferred value type is
# the join of three unrelated classes, which is `type[object]` - and
# object takes no constructor arguments.
_SCALARS: dict[str, Callable[[Any], Any]] = {"str": str, "int": int, "bool": bool}


class WireCodec:
    """Reads the manifest; encodes and decodes rpc fields."""

    def __init__(self, manifest: dict[str, Any],
                 bindings: ModuleType | None = None) -> None:
        self.manifest = manifest
        self._bindings: ModuleType | None = bindings
        self.kinds: dict[str, str] = {}
        self.fields: dict[str, list[list[str]]] = {}
        for group in ("wrappers", "returned_types"):
            for name, proto in manifest[group].items():
                self.kinds[name] = proto["wire"]
                self.fields[name] = proto["wire_fields"]

    @property
    def bindings(self) -> ModuleType:
        if self._bindings is None:
            self._bindings = importlib.import_module("fake_library")
        return self._bindings

    # -- classification ---------------------------------------------------
    def kind(self, type_str: str) -> str:
        """"none", "scalar", "map", "value" or "proxy"."""
        if type_str == "None":
            return "none"
        if type_str in SCALAR_NAMES:
            return "scalar"
        if map_value(type_str) is not None:
            return "map"
        try:
            return self.kinds[type_str]
        except KeyError:
            raise TypeError(
                f"{type_str!r} has no wire policy: it is neither a scalar nor "
                f"a class in the manifest") from None

    # -- wire-values ------------------------------------------------------
    @staticmethod
    def _sync(obj: Any) -> Any:
        """The sync binding object behind a possibly-async wrapper.

        The server holds generated wrappers, the client holds sync
        objects; the round-trip helpers live only on the sync class.
        Wire-values are always pool-threaded (the codegen enforces it),
        so unwrapping is safe from whichever thread we are on."""
        if getattr(obj, "_runner", None) is None:
            return obj
        from fake_library_generated._runtime import unwrap_arg
        return unwrap_arg(obj)

    def value_to_msg(self, type_str: str, obj: Any, msg: Any) -> None:
        """Fill msg from obj, one declared field at a time."""
        parts = self._sync(obj)._parts()
        declared = self.fields[type_str]
        if len(parts) != len(declared):
            raise TypeError(
                f"{type_str}._parts() returned {len(parts)} value(s) for "
                f"{len(declared)} declared _wire_fields")
        for (fname, ftype), val in zip(declared, parts, strict=True):
            optional = ftype.endswith("?")
            ftype = ftype.removesuffix("?")
            if val is None:
                if not optional:
                    raise TypeError(f"{type_str}.{fname} is not optional")
                continue  # proto3 default stands in for "unset"
            if self.kind(ftype) == "value":
                self.value_to_msg(ftype, val, getattr(msg, fname))
            else:
                setattr(msg, fname, val)

    # -- maps -------------------------------------------------------------
    # An attribute set has string keys, always, so `map<string, V>`
    # covers every dict this API returns (tasks/030). The value type
    # comes from the declaration - `dict[str, int]`, not a bare `dict`
    # - which is the same annotation the typechecker reads.
    def map_to_msg(self, type_str: str, obj: dict[str, Any], msg: Any) -> None:
        vtype = self._map_value(type_str)
        if self.kind(vtype) == "value":
            for key, val in obj.items():
                # A message-valued map entry is filled in place; there
                # is no assigning one.
                self.value_to_msg(vtype, val, msg[key])
        else:
            cast = _SCALARS[vtype]
            for key, val in obj.items():
                msg[key] = cast(val)

    def map_from_msg(self, type_str: str, msg: Any) -> dict[str, Any]:
        vtype = self._map_value(type_str)
        if self.kind(vtype) == "value":
            return {k: self.value_from_msg(vtype, v) for k, v in msg.items()}
        return dict(msg)

    @staticmethod
    def _map_value(type_str: str) -> str:
        vtype = map_value(type_str)
        if vtype is None:
            raise TypeError(f"{type_str!r} is not a map")
        return vtype

    # -- the recursive value message ----------------------------------
    # A value that holds values. Not built from a _wire_fields
    # declaration: the shape is recursive and its arms are the wire
    # KINDS themselves. What this module does NOT know is how to walk
    # one - that comes from a declaration next to the binding, through
    # the manifest, and arrives here already walked (tasks/030).
    #
    # The plain shape both sides speak:
    #   ("scalar", "int", 5)     a leaf, by declared type
    #   ("proxy", "Value", obj)  a node that stays remote
    #   ("list", [node, ...])
    #   ("attrs", {name: node})
    ARMS: ClassVar[dict[str, str]] = {
        "str": "s", "int": "i", "bool": "b", "float": "f"}

    def tree_to_msg(self, node: Any, msg: Any,
                    proxy_id: Callable[[str, Any], str]) -> None:
        """Fill a NixValue message from one walked node."""
        what = node[0]
        if what == "scalar":
            _, type_str, val = node
            try:
                arm = self.ARMS[type_str]
            except KeyError:
                raise TypeError(
                    f"{type_str!r} has no arm in the value message; it is "
                    f"not one of {sorted(self.ARMS)}") from None
            setattr(msg, arm, _SCALARS.get(type_str, float)(val))
        elif what == "proxy":
            _, cls, obj = node
            msg.proxy.handle.id = proxy_id(cls, obj)
            # A handle does not say what it is, and no layer above the
            # bindings may name a class. The walk knows, so it says.
            msg.proxy.cls = cls
        elif what == "list":
            # Touch the arm even when empty: proto3 would otherwise
            # leave the oneof unset and the far side could not tell an
            # empty list from a missing value.
            msg.list.SetInParent()
            for item in node[1]:
                self.tree_to_msg(item, msg.list.items.add(), proxy_id)
        elif what == "attrs":
            msg.attrs.SetInParent()
            for name, item in node[1].items():
                self.tree_to_msg(item, msg.attrs.entries[name], proxy_id)
        else:
            raise TypeError(f"unknown value-tree node {what!r}")

    def tree_from_msg(self, msg: Any,
                      proxy_obj: Callable[[str, str], Any]) -> Any:
        """Rebuild a Python value from a NixValue message.

        Scalars come back as themselves, a list as a list, an attribute
        set as a dict, and anything the far side would not serialize as
        a proxy object. Attribute names are sorted: Nix attribute sets
        are alphabetical and a protobuf map has no order, so the order
        is restored here rather than trusted."""
        arm = msg.WhichOneof("v")
        if arm is None:
            raise TypeError("value message carries no arm")
        if arm == "proxy":
            return proxy_obj(msg.proxy.cls, msg.proxy.handle.id)
        if arm == "list":
            return [self.tree_from_msg(i, proxy_obj) for i in msg.list.items]
        if arm == "attrs":
            return {k: self.tree_from_msg(msg.attrs.entries[k], proxy_obj)
                    for k in sorted(msg.attrs.entries)}
        return getattr(msg, arm)

    def value_from_msg(self, type_str: str, msg: Any) -> Any:
        """Rebuild a sync binding object from its message."""
        args = []
        for fname, ftype in self.fields[type_str]:
            optional = ftype.endswith("?")
            ftype = ftype.removesuffix("?")
            raw = getattr(msg, fname)
            if self.kind(ftype) == "value":
                args.append(self.value_from_msg(ftype, raw))
            else:
                # proto3 cannot distinguish unset from default, so the
                # "?" marker decides how to read an empty one back.
                args.append(None if optional and not raw else raw)
        return getattr(self.bindings, type_str)._from_parts(*args)

    # -- rpc fields -------------------------------------------------------
    def encode(self, container: Any, field: str, type_str: str, value: Any,
               proxy_id: Callable[[Any], str]) -> None:
        """Put `value` into `container.field`. proxy_id(value) -> handle
        id, called only for proxy types."""
        kind = self.kind(type_str)
        if kind == "none":
            return
        if kind == "scalar":
            setattr(container, field, _SCALARS[type_str](value))
        elif kind == "map":
            self.map_to_msg(type_str, value, getattr(container, field))
        elif kind == "value":
            self.value_to_msg(type_str, value, getattr(container, field))
        else:
            getattr(container, field).id = proxy_id(value)

    def decode(self, container: Any, field: str, type_str: str,
               proxy_obj: Callable[[str], Any],
               optional: bool = False) -> Any:
        """Read `container.field`. proxy_obj(handle_id) -> object,
        called only for proxy types.

        `optional` means an absent field reads back as None rather than
        as its default. proto3 tracks presence for message fields, so
        those are exact; a scalar cannot tell an unset string from an
        empty one, which is the same limitation _wire_fields marks with
        a trailing "?"."""
        kind = self.kind(type_str)
        if kind == "none":
            return None
        if optional and kind in ("value", "proxy") and not container.HasField(field):
            return None
        raw = getattr(container, field)
        if kind == "scalar":
            return None if optional and not raw else raw
        if kind == "map":
            return self.map_from_msg(type_str, raw)
        if kind == "value":
            return self.value_from_msg(type_str, raw)
        return proxy_obj(raw.id)
