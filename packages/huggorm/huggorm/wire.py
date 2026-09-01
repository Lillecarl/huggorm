"""
The wire codec, shared by the server and the client.

This module knows the SHAPE of the problem - scalars go in fields,
wire-values decompose into their declared parts, proxies travel as
handles - and nothing about the types. Every type name it acts on
comes from `huggorm_generated._policy`, which the build emitted from
the `_wire` / `_wire_fields` declaration next to the binding itself.

That is a RUNTIME, in this repo's sense: it does the same thing for
every type, so there is nothing in it a declaration could state. What
was derivable - which type crosses as what - is the table it reads.

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

from huggorm_generated._callspec import Arg
from huggorm_generated._policy import (
    ENUMS,
    ERROR_FIELDS,
    ERROR_MODULE,
    UNION_ARMS,
    WIRE_FIELDS,
    WIRE_KIND,
)
from huggorm_generated._wiretypes import (
    MAX_UNION_DEPTH,
    SCALAR_NAMES,
    arm_field,
    list_value,
    map_value,
    optional_value,
)

# The scalars as a lookup. Annotated because the inferred value type is
# the join of unrelated classes, which is `type[object]` - and object
# takes no constructor arguments.
#
# bytes() is deliberately NOT a converter that accepts anything: given
# a str it raises rather than guessing an encoding, which is the right
# answer for file contents whose hash names a store path.
_SCALARS: dict[str, Callable[[Any], Any]] = {
    "str": str, "int": int, "bool": bool, "bytes": bytes}


def _no_proxy(type_str: str, fname: str) -> Callable[[Any], Any]:
    """The arm a wire-value field can never take.

    encode() and decode() both need one, because an rpc field may
    carry a proxy. A _wire_fields entry may not: a wire-value is
    rebuilt from its parts on the far side, and a proxy is exactly the
    thing that has no object there. check_wire_contract says so at
    build time, so reaching this means the build let something
    through."""
    def refuse(_: Any) -> Any:
        raise TypeError(
            f"{type_str}.{fname} carries a proxy inside a wire value, which "
            f"the build refuses: a wire-value copies all the way down")
    return refuse


class WireCodec:
    """Reads the emitted wire policy; encodes and decodes rpc fields."""

    def __init__(self, bindings: ModuleType | None = None) -> None:
        """No arguments but the bindings, and that is the change.

        It took the manifest and unpacked four tables out of it. They
        are emitted now, in `huggorm_generated._policy`, so this reads
        them by name instead of digging them out of a JSON load - and
        a typechecker can see what each one holds.

        Read at import rather than copied per instance. They are
        constants; a copy would only be a chance for one to differ."""
        self._bindings: ModuleType | None = bindings
        # String vocabularies. A member is a str, so these cross as
        # scalars - the only thing this table changes is that a
        # decoded value comes back TYPED rather than as a bare str.
        self.enums: frozenset[str] = ENUMS
        # SUM types, {alias: (arm, ...)} in declared order - which is
        # the order the schema numbered the oneof's fields in.
        self.unions: dict[str, tuple[str, ...]] = UNION_ARMS
        self.kinds: dict[str, str] = WIRE_KIND
        self.fields: dict[str, tuple[Arg, ...]] = WIRE_FIELDS
        # EXCEPTION classes, and what each is rebuilt from. A value
        # may hold one: a KeyedBuildResult's failure arm IS a declared
        # error, answered rather than raised (tasks/071). Same table
        # the fault codec reads, because it is the same message.
        self.errors: dict[str, tuple[Arg, ...]] = ERROR_FIELDS
        self.error_module: str = ERROR_MODULE

    @property
    def bindings(self) -> ModuleType:
        if self._bindings is None:
            self._bindings = importlib.import_module("huggorm_bindings")
        return self._bindings

    def scalar(self, type_str: str) -> Callable[[Any], Any]:
        """What turns a raw scalar into the declared type.

        `str`, `int`, `bool` and `bytes` for the built-in ones, and the
        enum CLASS for a string vocabulary - so a value read off the
        wire arrives as ContentAddressMethod.FLAT rather than as
        "flat", and one that is not a member raises here instead of
        reaching libstore."""
        if type_str in self.enums:
            kls: Callable[[Any], Any] = getattr(self.bindings, type_str)
            return kls
        return _SCALARS[type_str]

    # -- classification ---------------------------------------------------
    @staticmethod
    def split_optional(type_str: str) -> tuple[str, bool]:
        """`T | None` as (T, True); anything else as (type_str, False).

        Absence is not a kind of its own. It is presence on the field
        the inner type would have had anyway, which is why this
        answers with that type and a flag rather than with a sixth
        kind."""
        inner = optional_value(type_str)
        return (inner, True) if inner is not None else (type_str, False)

    def kind(self, type_str: str) -> str:
        """"none", "scalar", "map", "list", "value", "error" or "proxy"."""
        if type_str == "None":
            return "none"
        if type_str in SCALAR_NAMES or type_str in self.enums:
            return "scalar"
        if type_str in self.unions:
            return "union"
        if type_str in self.errors:
            return "error"
        if map_value(type_str) is not None:
            return "map"
        if list_value(type_str) is not None:
            return "list"
        try:
            return self.kinds[type_str]
        except KeyError:
            raise TypeError(
                f"{type_str!r} has no wire policy: it is neither a scalar nor "
                f"a declared class") from None

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

    def value_to_msg(self, type_str: str, obj: Any, msg: Any,
                     depth: int = 0) -> None:
        """Fill msg from obj, one declared field at a time.

        A field is put the same way an rpc field is. It was its own
        two-armed dispatch - a nested value, else assign - which meant
        a declared `list[T]` reached `setattr` and a repeated protobuf
        field cannot be assigned. encode() already had every arm, so
        this asks it rather than growing a second copy."""
        parts = self._sync(obj)._parts()
        declared = self.fields[type_str]
        if len(parts) != len(declared):
            raise TypeError(
                f"{type_str}._parts() returned {len(parts)} value(s) for "
                f"{len(declared)} declared _wire_fields")
        for field, val in zip(declared, parts, strict=True):
            optional = field.type.endswith("?")
            ftype = field.type.removesuffix("?")
            if val is None:
                if not optional:
                    raise TypeError(f"{type_str}.{field.name} is not optional")
                continue  # proto3 default stands in for "unset"
            self.encode(msg, field.name, ftype, val,
                        _no_proxy(type_str, field.name), depth)

    # -- errors as fields -------------------------------------------------
    # An exception a VALUE holds, rather than one a call failed with.
    # The message is the same either way - `<Class>Fault`, carrying the
    # declared `_wire_fields` - so a peer that can rebuild a failure
    # can rebuild this, and there is one shape for one error class.
    def error_to_msg(self, type_str: str, err: Any, msg: Any) -> None:
        """Fill msg from an exception, one declared part at a time.

        The parts by NAME, not through `_parts()`: an exception is
        Python's own object and carries no such helper, and its parts
        are attributes the class already publishes."""
        for f in self.errors[type_str]:
            self.encode(msg, f.name, f.type, getattr(err, f.name),
                        _no_proxy(type_str, f.name))

    def error_from_msg(self, type_str: str, msg: Any) -> Any:
        """Rebuild the exception a message describes.

        `cls(*parts)` in declared order, which is why that order is
        the constructor's - the same rule the fault codec follows,
        and the same table.

        The class comes from the emitted error module and from
        nowhere else. A name off the wire never selects an importable
        class: it selects an entry in a table this build wrote
        (tasks/036)."""
        kls = getattr(importlib.import_module(self.error_module), type_str)
        return kls(*(self.decode(msg, f.name, f.type, _no_proxy(type_str, f.name))
                     for f in self.errors[type_str]))

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
            # self.scalar, not the _SCALARS table: an enum is a scalar
            # and its constructor is the enum CLASS, which that table
            # does not hold. Indexing it directly raised KeyError on
            # the first dict[str, HashAlgorithm] to be encoded - a
            # declaration the schema accepts (tasks/047).
            cast = self.scalar(vtype)
            for key, val in obj.items():
                msg[key] = cast(val)

    def map_from_msg(self, type_str: str, msg: Any) -> dict[str, Any]:
        vtype = self._map_value(type_str)
        if self.kind(vtype) == "value":
            return {k: self.value_from_msg(vtype, v) for k, v in msg.items()}
        # Cast on the way back too. `dict(msg)` kept the raw strs, so
        # an enum map arrived untyped - the quieter half of the same
        # bug, and the one that breaks 038's promise that a value read
        # off the wire comes back TYPED.
        cast = self.scalar(vtype)
        return {k: cast(v) for k, v in msg.items()}

    @staticmethod
    def _map_value(type_str: str) -> str:
        vtype = map_value(type_str)
        if vtype is None:
            raise TypeError(f"{type_str!r} is not a map")
        return vtype

    # -- lists ------------------------------------------------------------
    # A repeated field, which is the whole difference from a map: there
    # is no entry message, so a scalar list extends and a message list
    # adds. Both directions keep ORDER, unlike a map, because a
    # repeated field has one and Nix lists depend on it.
    def list_to_msg(self, type_str: str, seq: list[Any], field: Any,
                    depth: int = 0) -> None:
        itype = self._list_item(type_str)
        kind = self.kind(itype)
        if kind in ("value", "union"):
            fill = (self.value_to_msg if kind == "value"
                    else self.union_to_msg)
            for item in seq:
                # Same as a map entry: a message element is filled in
                # place, never assigned.
                fill(itype, item, field.add(), depth)
        else:
            cast = self.scalar(itype)
            field.extend(cast(v) for v in seq)

    def list_from_msg(self, type_str: str, field: Any,
                      depth: int = 0) -> list[Any]:
        itype = self._list_item(type_str)
        kind = self.kind(itype)
        if kind in ("value", "union"):
            read = (self.value_from_msg if kind == "value"
                    else self.union_from_msg)
            return [read(itype, m, depth) for m in field]
        cast = self.scalar(itype)
        return [cast(v) for v in field]

    @staticmethod
    def _list_item(type_str: str) -> str:
        itype = list_value(type_str)
        if itype is None:
            raise TypeError(f"{type_str!r} is not a list")
        return itype

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

    def value_from_msg(self, type_str: str, msg: Any,
                       depth: int = 0) -> Any:
        """Rebuild a sync binding object from its message.

        The mirror of value_to_msg, and it delegates for the same
        reason. decode() already knows that a message field has
        presence - so an optional nested value reads back as None
        rather than as a StorePath rebuilt from an empty base name -
        and that a repeated field is read, not fetched."""
        args = [
            self.decode(msg, f.name, f.type.removesuffix("?"),
                        _no_proxy(type_str, f.name),
                        optional=f.type.endswith("?"), depth=depth)
            for f in self.fields[type_str]
        ]
        return getattr(self.bindings, type_str)._from_parts(*args)

    # -- unions -----------------------------------------------------------
    #
    # A oneof, which is a real tag - unlike either encoding upstream
    # uses. The daemon sends a DerivedPath as a string and parses it
    # back; Nix's JSON tags the arms by shape. Here the arm is named
    # in the message and neither side guesses (tasks/059).
    def union_to_msg(self, type_str: str, obj: Any, msg: Any,
                     depth: int = 0) -> None:
        """Fill msg's oneof from whichever arm `obj` is.

        By isinstance over the declared arms, in order. The arms are
        distinct bound classes - the reader refuses a scalar or a
        vocabulary arm precisely so that this test can be exact - so
        the first match is the only match."""
        self._not_too_deep(type_str, depth)
        held = self._sync(obj)
        for arm in self.unions[type_str]:
            if isinstance(held, getattr(self.bindings, arm)):
                self.value_to_msg(arm, held, getattr(msg, arm_field(arm)),
                                  depth + 1)
                return
        raise TypeError(
            f"{type(obj).__name__} is not one of {type_str}'s arms "
            f"({', '.join(self.unions[type_str])})")

    def union_from_msg(self, type_str: str, msg: Any, depth: int = 0) -> Any:
        """Rebuild whichever arm the message carries.

        `WhichOneof` names it, so nothing is inferred from shape."""
        self._not_too_deep(type_str, depth)
        which = msg.WhichOneof("raw")
        if which is None:
            raise ValueError(
                f"a {type_str} arrived with no arm set. Every one of "
                f"{', '.join(self.unions[type_str])} would have named "
                f"itself, so this message was not written by a peer that "
                f"read the same schema.")
        for arm in self.unions[type_str]:
            if arm_field(arm) == which:
                return self.value_from_msg(arm, getattr(msg, which),
                                           depth + 1)
        raise ValueError(f"{type_str} has no arm called {which!r}")

    def _not_too_deep(self, type_str: str, depth: int) -> None:
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
                f"a {type_str} nested more than {MAX_UNION_DEPTH} deep. "
                f"A derived path names an output of an output, which is "
                f"one or two levels in anything real - so this is a "
                f"malformed or hostile message rather than a deep one.")

    # -- rpc fields -------------------------------------------------------
    def encode(self, container: Any, field: str, type_str: str, value: Any,
               proxy_id: Callable[[Any], str], depth: int = 0) -> None:
        """Put `value` into `container.field`. proxy_id(value) -> handle
        id, called only for proxy types."""
        type_str, optional = self.split_optional(type_str)
        kind = self.kind(type_str)
        if kind == "none":
            return
        if value is None and (optional or kind in ("map", "list")):
            # Two absences, one answer: write nothing.
            #
            # A declared `T | None` leaves its message field unset, and
            # proto3 tracks that, so the far side reads it back as
            # None. A container parameter defaults to None and a
            # repeated field has no presence - writing nothing IS
            # writing an empty one, which is what None means for a
            # container (tasks/041).
            return
        if kind == "scalar":
            # str() of a StrEnum member is its value, so an enum needs
            # no special case going out.
            setattr(container, field, self.scalar(type_str)(value))
        elif kind == "map":
            self.map_to_msg(type_str, value, getattr(container, field))
        elif kind == "list":
            self.list_to_msg(type_str, value, getattr(container, field), depth)
        elif kind == "value":
            self.value_to_msg(type_str, value, getattr(container, field),
                              depth)
        elif kind == "union":
            self.union_to_msg(type_str, value, getattr(container, field),
                              depth)
        elif kind == "error":
            self.error_to_msg(type_str, value, getattr(container, field))
        else:
            getattr(container, field).id = proxy_id(value)

    def decode(self, container: Any, field: str, type_str: str,
               proxy_obj: Callable[[str], Any],
               optional: bool = False, depth: int = 0) -> Any:
        """Read `container.field`. proxy_obj(handle_id) -> object,
        called only for proxy types.

        `optional` means an absent field reads back as None rather than
        as its default, and it is EXACT for every kind. A message field
        has presence in proto3; a scalar one gets it from the synthetic
        oneof the schema builder emits for a declared optional
        (tasks/048). So HasField answers both.

        It did not always. A scalar read back as `None if not raw`,
        which cannot tell an unset string from an empty one someone
        meant - so an explicitly-passed "" became None. That was the
        limitation this codec had, not one proto3 has.

        A `T | None` type says the same thing in the annotation rather
        than in the call, so the two are OR-ed: a caller that already
        knows the field is optional need not read the type, and a type
        that says so need not be told twice."""
        type_str, declared = self.split_optional(type_str)
        optional = optional or declared
        kind = self.kind(type_str)
        if kind == "none":
            return None
        # A container is the one kind with nothing to ask: a repeated
        # field has no presence and needs none.
        if (optional and kind not in ("list", "map")
                and not container.HasField(field)):
            return None
        raw = getattr(container, field)
        if kind == "scalar":
            return self.scalar(type_str)(raw)
        if kind == "map":
            return self.map_from_msg(type_str, raw)
        if kind == "list":
            return self.list_from_msg(type_str, raw, depth)
        if kind == "value":
            return self.value_from_msg(type_str, raw, depth)
        if kind == "union":
            return self.union_from_msg(type_str, raw, depth)
        if kind == "error":
            return self.error_from_msg(type_str, raw)
        return proxy_obj(raw.id)
