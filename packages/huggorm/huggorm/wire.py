"""
The wire codec, shared by the server and the client.

This module knows the SHAPE of the problem - scalars go in fields,
wire-values decompose into their declared parts, proxies travel as
handles - and nothing about the types. Every type it acts on arrives
as a `Wire`, which the build emitted from the declaration next to the
binding, so the codec reads no type string and resolves no name.

That is a RUNTIME, in this repo's sense: it does the same thing for
every type, so there is nothing in it a declaration could state.

The two sides differ in exactly one respect, so it is the one thing
they pass in: what a proxy handle means. The server resolves an id to a
live wrapper and registers new ones; the client turns an id into a
RemoteObj and reads ids back off one.
"""

from __future__ import annotations

import datetime
import importlib
from collections.abc import Callable
from types import ModuleType
from typing import Any

from huggorm_generated._callspec import Arg, Wire
from huggorm_generated._policy import (
    ERROR_FIELDS,
    ERROR_MODULE,
    UNION_ARMS,
    WIRE_FIELDS,
)
from huggorm_generated._wiretypes import MAX_UNION_DEPTH, TREE_ARMS, arm_field

# The scalars as a lookup. Annotated because the inferred value type is
# the join of unrelated classes, which is `type[object]` - and object
# takes no constructor arguments.
#
# bytes() is deliberately NOT a converter that accepts anything: given
# a str it raises rather than guessing an encoding, which is the right
# answer for file contents whose hash names a store path.
# `int` and `uint` are both Python's int. The two differ only in the
# proto type the field has, which is the schema's business and not
# this table's (huggorm#79).
_SCALARS: dict[str, Callable[[Any], Any]] = {
    "str": str, "int": int, "uint": int, "float": float, "bool": bool,
    "bytes": bytes}

# The unit a duration crosses in, as the timedelta that is one of it.
# Microseconds, which is both Carl's decision and the only lossless
# answer: a timedelta's finest unit IS the microsecond, and upstream
# keeps a build's CPU time as std::chrono::microseconds.
_MICROSECOND = datetime.timedelta(microseconds=1)


def _duration_out(value: datetime.timedelta) -> int:
    """A duration as the whole microseconds a field carries.

    Floor division by one microsecond, which is exact: a timedelta
    holds days, seconds and microseconds as integers, so there is no
    remainder to lose."""
    return value // _MICROSECOND


def _duration_in(raw: int) -> datetime.timedelta:
    """The microseconds a field carried, as a duration again."""
    return datetime.timedelta(microseconds=raw)


# What a SPELLED scalar becomes at each end. `_wiretypes.SPELLED` says
# which builtin field one goes in; this says what to put there and what
# to make of it coming back, which is the half only the codec needs.
#
# Two entries per type rather than one, unlike a vocabulary: a StrEnum
# member IS a str, so one constructor serves both directions. A
# timedelta is not an int, so the two directions differ.
_SPELLED: dict[str, tuple[Callable[[Any], Any], Callable[[Any], Any]]] = {
    "datetime.timedelta": (_duration_out, _duration_in),
}

# The kinds that go in a field as one scalar. A StrEnum member is a
# str, so a vocabulary is one.
_FLAT = ("scalar", "enum")


def _no_proxy(owner: str, fname: str) -> Callable[[Any], Any]:
    """The arm a wire-value field can never take.

    encode() and decode() both need one, because an rpc field may
    carry a proxy. A part may not: a wire-value is rebuilt from its
    parts on the far side, and a proxy is exactly the thing that has
    no object there. `contracts.wire` says so at build time, so
    reaching this means the build let something through."""
    def refuse(_: Any) -> Any:
        raise TypeError(
            f"{owner}.{fname} carries a proxy inside a wire value, which "
            f"the build refuses: a wire-value copies all the way down")
    return refuse


class WireCodec:
    """Reads the emitted wire policy; encodes and decodes rpc fields."""

    def __init__(self, bindings: ModuleType | None = None) -> None:
        """The tables are emitted, in `huggorm_generated._policy`, and
        read at import rather than copied per instance."""
        self._bindings: ModuleType | None = bindings
        # SUM types, {alias: (arm, ...)} in declared order - which is
        # the order the schema numbered the oneof's fields in.
        self.unions: dict[str, tuple[Wire, ...]] = UNION_ARMS
        self.fields: dict[str, tuple[Arg, ...]] = WIRE_FIELDS
        # EXCEPTION classes, and what each is rebuilt from. A value
        # may hold one: a KeyedBuildResult's failure arm IS a declared
        # error, answered rather than raised (huggorm#71). Same table
        # the fault codec reads, because it is the same message.
        self.errors: dict[str, tuple[Arg, ...]] = ERROR_FIELDS
        self.error_module: str = ERROR_MODULE

    @property
    def bindings(self) -> ModuleType:
        if self._bindings is None:
            self._bindings = importlib.import_module("huggorm_bindings")
        return self._bindings

    def scalar(self, w: Wire) -> Callable[[Any], Any]:
        """What turns a raw scalar into the declared type.

        `str`, `int`, `bool` and `bytes` for the built-in ones, and the
        enum CLASS for a string vocabulary - so a value read off the
        wire arrives as ContentAddressMethod.FLAT rather than as
        "flat", and one that is not a member raises here instead of
        reaching libstore."""
        if w.kind == "enum":
            kls: Callable[[Any], Any] = getattr(self.bindings, w.name)
            return kls
        if (spelled := _SPELLED.get(w.name)) is not None:
            return spelled[1]
        return _SCALARS[w.name]

    def to_wire(self, w: Wire) -> Callable[[Any], Any]:
        """What turns a declared value into the raw scalar it goes as.

        The other direction of `scalar`, and the same function for
        almost everything: `str(x)` of a StrEnum member is its value,
        and an int is an int. A SPELLED scalar is where the two part -
        a datetime.timedelta goes in an int field, and an int does not
        come back as a timedelta by itself."""
        if (spelled := _SPELLED.get(w.name)) is not None:
            return spelled[0]
        return self.scalar(w)

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
        from huggorm_generated._runtime import unwrap_arg
        return unwrap_arg(obj)

    def value_to_msg(self, cls: str, obj: Any, msg: Any,
                     depth: int = 0) -> None:
        """Fill msg from obj, one declared field at a time, each put
        the way an rpc field is."""
        parts = self._sync(obj)._parts()
        declared = self.fields[cls]
        if len(parts) != len(declared):
            raise TypeError(
                f"{cls}._parts() returned {len(parts)} value(s) for "
                f"{len(declared)} declared _wire_fields")
        for field, val in zip(declared, parts, strict=True):
            if val is None and not field.type.optional:
                raise TypeError(f"{cls}.{field.name} is not optional")
            self.encode(msg, field.name, field.type, val,
                        _no_proxy(cls, field.name), depth)

    def value_from_msg(self, cls: str, msg: Any, depth: int = 0) -> Any:
        """Rebuild a sync binding object from its message.

        Through decode(), which knows that a message field has presence
        - so an optional nested value reads back as None rather than as
        a StorePath rebuilt from an empty base name - and that a
        repeated field is read, not fetched."""
        args = [self.decode(msg, f.name, f.type, _no_proxy(cls, f.name),
                            depth=depth)
                for f in self.fields[cls]]
        return getattr(self.bindings, cls)._from_parts(*args)

    # -- errors as fields -------------------------------------------------
    # An exception a VALUE holds, rather than one a call failed with.
    # The message is the same either way - `<Class>Fault`, carrying the
    # declared parts - so there is one shape for one error class.
    def error_to_msg(self, cls: str, err: Any, msg: Any) -> None:
        """Fill msg from an exception, one declared part at a time.

        The parts by NAME, not through `_parts()`: an exception is
        Python's own object and carries no such helper, and its parts
        are attributes the class already publishes."""
        for f in self.errors[cls]:
            self.encode(msg, f.name, f.type, getattr(err, f.name),
                        _no_proxy(cls, f.name))

    def error_from_msg(self, cls: str, msg: Any) -> Any:
        """Rebuild the exception a message describes, as `cls(*parts)`.

        The class comes from the emitted error module and from
        nowhere else. A name off the wire never selects an importable
        class: it selects an entry in a table this build wrote
        (huggorm#36)."""
        kls = getattr(importlib.import_module(self.error_module), cls)
        return kls(*self.error_parts(cls, msg))

    def error_parts(self, cls: str, msg: Any) -> list[Any]:
        """An exception's parts off its message, in constructor order.

        Shared with the fault codec, which takes the class from a
        module of its own and the parts from here."""
        return [self.decode(msg, f.name, f.type, _no_proxy(cls, f.name))
                for f in self.errors[cls]]

    # -- maps -------------------------------------------------------------
    # An attribute set has string keys, always, so `map<string, V>`
    # covers every dict this API returns (huggorm#30).
    def map_to_msg(self, w: Wire, obj: dict[str, Any], msg: Any) -> None:
        item = self._item(w)
        if item.kind in ("value", "union"):
            fill = (self.value_to_msg if item.kind == "value"
                    else self.union_to_msg)
            for key, val in obj.items():
                # A message-valued map entry is filled in place; there
                # is no assigning one.
                fill(item.name, val, msg[key])
        else:
            # to_wire, not the _SCALARS table: an enum's constructor is
            # the enum CLASS, which that table does not hold (huggorm#47).
            cast = self.to_wire(item)
            for key, val in obj.items():
                msg[key] = cast(val)

    def map_from_msg(self, w: Wire, msg: Any) -> dict[str, Any]:
        item = self._item(w)
        if item.kind in ("value", "union"):
            read = (self.value_from_msg if item.kind == "value"
                    else self.union_from_msg)
            return {k: read(item.name, v) for k, v in msg.items()}
        # Cast on the way back too, so an enum map arrives typed.
        cast = self.scalar(item)
        return {k: cast(v) for k, v in msg.items()}

    # -- lists ------------------------------------------------------------
    # A repeated field, which is the whole difference from a map: there
    # is no entry message, so a scalar list extends and a message list
    # adds. Both directions keep ORDER, unlike a map, because a
    # repeated field has one and Nix lists depend on it.
    def list_to_msg(self, w: Wire, seq: list[Any], field: Any,
                    depth: int = 0) -> None:
        item = self._item(w)
        if item.kind in ("value", "union"):
            fill = (self.value_to_msg if item.kind == "value"
                    else self.union_to_msg)
            for one in seq:
                # Same as a map entry: a message element is filled in
                # place, never assigned.
                fill(item.name, one, field.add(), depth)
        else:
            cast = self.to_wire(item)
            field.extend(cast(v) for v in seq)

    def list_from_msg(self, w: Wire, field: Any,
                      depth: int = 0) -> list[Any]:
        item = self._item(w)
        if item.kind in ("value", "union"):
            read = (self.value_from_msg if item.kind == "value"
                    else self.union_from_msg)
            return [read(item.name, m, depth) for m in field]
        cast = self.scalar(item)
        return [cast(v) for v in field]

    @staticmethod
    def _item(w: Wire) -> Wire:
        if w.item is None:
            raise TypeError(f"a {w.kind} Wire with no item")
        return w.item

    # -- the recursive value message ----------------------------------
    # A value that holds values. Not built from declared parts: the
    # shape is recursive and its arms are the wire KINDS themselves.
    # What this module does NOT know is how to walk one - that comes
    # from a declaration next to the binding and arrives here already
    # walked (huggorm#30).
    #
    # The plain shape both sides speak:
    #   ("scalar", "int", 5)     a leaf, by declared type
    #   ("proxy", "Value", obj)  a node that stays remote
    #   ("list", [node, ...])
    #   ("attrs", {name: node})
    def tree_to_msg(self, node: Any, msg: Any,
                    proxy_id: Callable[[str, Any], str]) -> None:
        """Fill a NixValue message from one walked node."""
        what = node[0]
        if what == "scalar":
            _, type_str, val = node
            try:
                arm = TREE_ARMS[type_str]
            except KeyError:
                raise TypeError(
                    f"{type_str!r} has no arm in the value message; it is "
                    f"not one of {sorted(TREE_ARMS)}") from None
            setattr(msg, arm, _SCALARS[type_str](val))
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

    # -- unions -----------------------------------------------------------
    #
    # A oneof, which is a real tag - unlike either encoding upstream
    # uses. The daemon sends a DerivedPath as a string and parses it
    # back; Nix's JSON tags the arms by shape. Here the arm is named
    # in the message and neither side guesses (huggorm#59).
    def union_to_msg(self, alias: str, obj: Any, msg: Any,
                     depth: int = 0) -> None:
        """Fill msg's oneof from whichever arm `obj` is.

        By isinstance over the declared arms, in order. The reader
        refuses a vocabulary arm and two scalar arms of one Python type,
        so the first match is the only match.

        A scalar arm matches its EXACT type. `True` is an `int` to
        isinstance, and a bool sent as the int arm arrives as 1."""
        self._not_too_deep(alias, depth)
        held = self._sync(obj)
        for arm in self.unions[alias]:
            if arm.kind in _FLAT:
                # `object`: a scalar's constructor IS its type here.
                exact: object = self.scalar(arm)
                if type(held) is exact:
                    setattr(msg, arm_field(arm.name), self.to_wire(arm)(held))
                    return
            elif isinstance(held, getattr(self.bindings, arm.name)):
                sub = getattr(msg, arm_field(arm.name))
                self.value_to_msg(arm.name, held, sub, depth + 1)
                # A unit arm writes no field, and protobuf sets a oneof
                # only when its message is written: `GCWholeStore`
                # arrived with no arm set (huggorm#55).
                sub.SetInParent()
                return
        raise TypeError(
            f"{type(obj).__name__} is not one of {alias}'s arms "
            f"({', '.join(a.name for a in self.unions[alias])})")

    def union_from_msg(self, alias: str, msg: Any, depth: int = 0) -> Any:
        """Rebuild whichever arm the message carries.

        `WhichOneof` names it, so nothing is inferred from shape."""
        self._not_too_deep(alias, depth)
        which = msg.WhichOneof("raw")
        if which is None:
            raise ValueError(
                f"a {alias} arrived with no arm set. Every one of "
                f"{', '.join(a.name for a in self.unions[alias])} would "
                f"have named itself, so this message was not written by a "
                f"peer that read the same schema.")
        for arm in self.unions[alias]:
            if arm_field(arm.name) != which:
                continue
            if arm.kind in _FLAT:
                return self.scalar(arm)(getattr(msg, which))
            return self.value_from_msg(arm.name, getattr(msg, which),
                                       depth + 1)
        raise ValueError(f"{alias} has no arm called {which!r}")

    def _not_too_deep(self, alias: str, depth: int) -> None:
        """Refuse a union nested deeper than anything real.

        A union arm may hold the union again - that is what lets a
        SingleDerivedPath name the output of a derivation that is
        itself an output - so a peer can send a chain as long as it
        likes and Python answers with a RecursionError, which reaches
        a caller as an anonymous InternalError.

        The wire is a trust boundary, so the limit is here rather than
        in a declaration: depth is a fact about CROSSING, not about
        the type. Real chains are one or two deep; the cap is
        generous so that only an attack or a bug reaches it."""
        if depth > MAX_UNION_DEPTH:
            raise ValueError(
                f"a {alias} nested more than {MAX_UNION_DEPTH} deep. "
                f"A derived path names an output of an output, which is "
                f"one or two levels in anything real - so this is a "
                f"malformed or hostile message rather than a deep one.")

    # -- rpc fields -------------------------------------------------------
    def encode(self, container: Any, field: str, w: Wire | None, value: Any,
               proxy_id: Callable[[Any], str], depth: int = 0) -> None:
        """Put `value` into `container.field`. proxy_id(value) -> handle
        id, called only for a proxy. `w` is None for a call that
        returns nothing."""
        if w is None:
            return
        if value is None and (w.optional or w.kind in ("map", "list")):
            # Two absences, one answer: write nothing.
            #
            # An optional leaves its message field unset, and proto3
            # tracks that, so the far side reads it back as None. A
            # container parameter defaults to None and a repeated field
            # has no presence - writing nothing IS writing an empty
            # one, which is what None means for a container
            # (huggorm#41).
            return
        if w.kind in _FLAT:
            # str() of a StrEnum member is its value, so an enum needs
            # no special case going out.
            setattr(container, field, self.to_wire(w)(value))
        elif w.kind == "map":
            self.map_to_msg(w, value, getattr(container, field))
        elif w.kind == "list":
            self.list_to_msg(w, value, getattr(container, field), depth)
        elif w.kind == "value":
            self.value_to_msg(w.name, value, getattr(container, field), depth)
        elif w.kind == "union":
            self.union_to_msg(w.name, value, getattr(container, field), depth)
        elif w.kind == "error":
            self.error_to_msg(w.name, value, getattr(container, field))
        elif w.kind == "proxy":
            getattr(container, field).id = proxy_id(value)
        else:
            raise TypeError(f"{w.kind!r} is not a wire kind")

    def decode(self, container: Any, field: str, w: Wire | None,
               proxy_obj: Callable[[str], Any],
               optional: bool = False, depth: int = 0) -> Any:
        """Read `container.field`. proxy_obj(handle_id) -> object,
        called only for a proxy.

        An optional field reads back as None when it is absent, and
        that is EXACT for every kind: a message field has presence in
        proto3, and a scalar one gets it from the synthetic oneof the
        schema builder emits for it (huggorm#48). So HasField answers
        both. `optional` is for a caller that knows what the type does
        not say - a constructor parameter whose default is None."""
        if w is None:
            return None
        optional = optional or w.optional
        # A container is the one kind with nothing to ask: a repeated
        # field has no presence and needs none.
        if (optional and w.kind not in ("list", "map")
                and not container.HasField(field)):
            return None
        raw = getattr(container, field)
        if w.kind in _FLAT:
            return self.scalar(w)(raw)
        if w.kind == "map":
            return self.map_from_msg(w, raw)
        if w.kind == "list":
            return self.list_from_msg(w, raw, depth)
        if w.kind == "value":
            return self.value_from_msg(w.name, raw, depth)
        if w.kind == "union":
            return self.union_from_msg(w.name, raw, depth)
        if w.kind == "error":
            return self.error_from_msg(w.name, raw)
        if w.kind == "proxy":
            return proxy_obj(raw.id)
        raise TypeError(f"{w.kind!r} is not a wire kind")
