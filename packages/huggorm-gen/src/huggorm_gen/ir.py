"""The typed model every generator stage reads (huggorm#29).

The reader hands back structured types: `read.Type` knows its origin,
its arguments and its leaf. This module keeps that structure and adds
what a type IS - a scalar, a vocabulary, a union, an error, a value or
a proxy - resolved ONCE, against the declaration set. A name nothing
declares is refused here, when the model is built, instead of in an
emitter that would have guessed.

Each rule a stage needs is a property here, stated once. The manifest
dict is a VIEW of this model (`ClassModel.entry`), kept while the
emitters move onto the model one at a time.
"""

from __future__ import annotations

import copy
import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from huggorm_dsl.declare import Decl
from huggorm_dsl.read import Class, Method, Module, Param, Type, is_surface
from huggorm_gen.payload.wiretypes import SCALAR_NAMES

# The words a proxy's RPC surface is spelled with. Every name below is
# the class name plus one of these.
PROTO_PACKAGE = "huggorm.v1"
SERVICE = "Service"
ACQUIRE = "Acquire"
PROTOCOL = "Like"
ASYNC = "Async"
RPC = "RPC"

# C++ spelling -> the Python type a caller sees. `bint` and
# `string_view` have no place above the binding: a caller holds a
# `bool` and a `str`.
PYTHON = {
    "string": "str",
    # A view is a str by the time it reaches Python: the binding
    # copies it, because a view outliving its owner is a dangling
    # pointer rather than an exception.
    "string_view": "str",
    "bint": "bool",
    # Widths. Python has one integer type, so both read as `int` - the
    # width is a fact about the crossing, not about the value a caller
    # holds.
    "uint64_t": "int",
    "int64_t": "int",
    "double": "float",
    # A span, and Python has a type for one: nanobind's chrono caster
    # hands a timedelta over already.
    "microseconds": "datetime.timedelta",
    # A reference to a Python object: nothing is marshalled either way,
    # and the binding holds the reference to call back through
    # (huggorm#33).
    "nb::object": "object",
}

# The value dunders, and the fact about the declaration that makes a
# class define each one. `!=` comes with `__eq__`, and the three
# comparisons `functools.total_ordering` writes come with `order=`.
DUNDERS: tuple[tuple[str, str], ...] = (
    ("__eq__", "value"),
    ("__ne__", "value"),
    ("__hash__", "value"),
    ("__repr__", "value"),
    ("__lt__", "order"),
    ("__le__", "order"),
    ("__gt__", "order"),
    ("__ge__", "order"),
    ("__str__", "text"),
)

# The C++ spellings a service's message cannot carry. A field says its
# width - `_wire_fields` spells `uint` - and a parameter does not, so a
# uint64_t on a service would cross as a sint64 and lose half its range.
# Refused, because nothing declares one (huggorm#79). A type that should
# never cross belongs in `grpc_schema.NOT_DATA`, which reports instead.
UNCROSSABLE = {
    "uint64_t": (
        "a service's message carries no width: every int parameter "
        "crosses as sint64, which holds half of one. Teach the "
        "manifest to spell a parameter's wire type - see huggorm#79."),
}

Kind = Literal["scalar", "enum", "union", "error", "value", "proxy",
               "module", "opaque"]


@dataclass(frozen=True)
class Resolver:
    """What each name a declaration set can write IS.

    Built from a reader `Module` - its `known` already names every
    class, union, vocabulary and error that module can see, its
    imports' included - so the corpus and a test's one-file
    declaration resolve the same way."""

    known: Mapping[str, Class]

    @classmethod
    def of(cls, module: Module) -> Resolver:
        return cls(module.known)

    def kind(self, name: str) -> Kind:
        if name in SCALAR_NAMES:
            return "scalar"
        if "." in name:
            # `pathlib.Path`, `datetime.timedelta`: a type in a module,
            # imported as itself.
            return "module"
        if name == "object":
            return "opaque"
        cls = self.known.get(name)
        if cls is None:
            raise TypeError(
                f"'{name}' names nothing this declaration set declares, "
                f"and it is not a builtin")
        if cls.decl.kind == "error":
            return "error"
        if cls.is_words:
            return "enum"
        if cls.is_union:
            return "union"
        return "value" if cls.decl.wire == "value" else "proxy"


@dataclass(frozen=True)
class TypeRef:
    """One declared type, resolved.

    `spelling` is the annotation a caller sees. `origin` and `args`
    are the structure ("optional", "list", "dict", or "" for a leaf),
    and `kind` and `name` say what the leaf is."""

    spelling: str
    origin: str
    args: tuple[TypeRef, ...]
    kind: Kind
    name: str

    @property
    def optional(self) -> bool:
        return self.origin == "optional"

    @property
    def required(self) -> TypeRef:
        """This type without the None, or itself."""
        return self.args[0] if self.optional else self

    @property
    def leaf(self) -> TypeRef:
        t = self
        while t.args:
            t = t.args[0]
        return t


def _spelling(t: Type) -> str:
    """The Python spelling of a declared type.

    A type with no C++ behind it is already Python. For one that has,
    an unknown C++ spelling is refused rather than guessed: the
    surfaces above could not marshal it."""
    leaf = t.leaf
    if leaf.cxx is None:
        return t.python
    if leaf.cxx.spelling not in PYTHON:
        raise TypeError(
            f"'{leaf.cxx.spelling}' has no Python spelling. Add it to "
            f"ir.PYTHON once the boundary knows how to marshal it.")
    # The DECLARATION's spelling wins where the two disagree: a
    # std::string is a `str` most of the time, and `bytes` or a
    # `pathlib.Path` where the alias says so.
    return t.python if t.python != "bool" else PYTHON[leaf.cxx.spelling]


def type_ref(t: Type, resolver: Resolver) -> TypeRef:
    leaf = t.leaf
    return TypeRef(
        spelling=_spelling(t),
        origin=t.origin,
        args=tuple(type_ref(a, resolver) for a in t.args),
        kind=resolver.kind(_spelling(leaf)),
        name=_spelling(leaf),
    )


def crossable(t: Type | None, where: str) -> None:
    """Refuse a type a service's message cannot spell.

    For what a SERVICE carries - a proxy's methods and the parameters
    that acquire one - and not for a value's accessors, which cross as
    fields that say their width."""
    leaf = t.leaf if t is not None else None
    if (leaf is not None and leaf.cxx is not None
            and leaf.cxx.spelling in UNCROSSABLE):
        raise TypeError(
            f"{where} is a {leaf.cxx.spelling}, and "
            f"{UNCROSSABLE[leaf.cxx.spelling]}")


def _clean(text: str) -> str:
    return inspect.cleandoc(text) if text else ""


@dataclass(frozen=True)
class ParamModel:
    name: str
    type: TypeRef
    # The default as the Python SOURCE that writes it, or None when
    # there is no default. A vocabulary member is written as the
    # member: `'nar'` says nothing about which vocabulary it is from.
    default: str | None

    @classmethod
    def of(cls, p: Param, resolver: Resolver) -> ParamModel:
        declared = p.type
        # A LIST that defaults to None is the list: an absent list IS
        # an empty one, and a repeated field has no presence to say
        # otherwise. Carried as `list[X] | None`, the schema refuses it
        # and the method loses its rpc in silence (huggorm#104).
        if (p.has_default and p.default is None
                and declared.required.origin == "list"):
            declared = declared.required
        if not p.has_default:
            default = None
        elif p.member:
            default = f"{p.type.python}.{p.member}"
        else:
            default = repr(p.default)
        return cls(p.name, type_ref(declared, resolver), default)

    def entry(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type.spelling,
                "default": self.default}


@dataclass(frozen=True)
class MethodModel:
    name: str
    params: tuple[ParamModel, ...]
    returns: TypeRef | None
    doc: str

    @classmethod
    def of(cls, m: Method, resolver: Resolver) -> MethodModel:
        return cls(m.name,
                   tuple(ParamModel.of(p, resolver) for p in m.params),
                   type_ref(m.ret, resolver) if m.ret is not None else None,
                   _clean(m.doc))

    @property
    def return_spelling(self) -> str:
        return self.returns.spelling if self.returns is not None else "None"

    def entry(self) -> dict[str, Any]:
        return {"name": self.name,
                "params": [p.entry() for p in self.params],
                "return_type": self.return_spelling,
                "doc": self.doc}


@dataclass(frozen=True)
class FunctionModel:
    """One free function. No policy means it opted into nothing: it
    gets no async form and no rpc, and it is still surface."""

    name: str
    module: str
    threading: str | None
    params: tuple[ParamModel, ...]
    returns: TypeRef | None
    doc: str

    @classmethod
    def of(cls, fn: Method, package: str, module: str,
           resolver: Resolver) -> FunctionModel:
        policy = fn.policy or None
        if policy is not None:
            for pr in fn.params:
                crossable(pr.type, f"{fn.name}({pr.name})")
            crossable(fn.ret, f"{fn.name}'s return")
        return cls(fn.name, f"{package}.{module}", policy,
                   tuple(ParamModel.of(p, resolver) for p in fn.params),
                   type_ref(fn.ret, resolver) if fn.ret is not None else None,
                   _clean(fn.doc))

    @property
    def wrapped(self) -> bool:
        return self.threading is not None

    def entry(self) -> dict[str, Any]:
        return {"name": self.name, "module": self.module,
                "threading": self.threading, "wrapped": self.wrapped,
                "params": [p.entry() for p in self.params],
                "return_type": (self.returns.spelling
                                if self.returns is not None else "None"),
                "doc": self.doc}


def dunders(decl: Decl) -> list[str]:
    """The value dunders a declaration implies, sorted."""
    facts = {"value": decl.wire == "value", "order": bool(decl.order),
             "text": bool(decl.text)}
    return sorted(name for name, fact in DUNDERS if facts[fact])


def _ctor_params(cls: Class,
                 functions: Sequence[Method] = ()) -> tuple[Param, ...]:
    """The parameters a caller passes to build one of these.

    A `@produced(by=X)` class that declares `__init__` is built by X,
    so X's parameters - defaults included - are the signature. Without
    an `__init__`, X is a function whose ANSWER is this class, and a
    caller never passes X's parameters to build one."""
    if cls.decl.built_by and cls.ctor is not None:
        made = next((f for f in functions if f.name == cls.decl.built_by),
                    None)
        if made is not None:
            return tuple(made.params)
    return tuple(cls.ctor.params) if cls.ctor is not None else ()


@dataclass(frozen=True)
class ClassModel:
    """One declared class, with every fact a stage reads."""

    name: str
    package: str
    module: str
    doc: str
    decl: Decl
    is_value: bool
    produced: bool
    constructs: bool
    wire_fields: tuple[tuple[str, str], ...]
    ctor: tuple[ParamModel, ...]
    methods: tuple[MethodModel, ...]

    @classmethod
    def of(cls, c: Class, package: str, module: str, resolver: Resolver,
           functions: Sequence[Method] = ()) -> ClassModel:
        decl = c.decl
        if (decl.wire or "proxy") == "proxy":
            # A proxy is reached through a service, so its methods ARE
            # messages. A value crosses whole, as its fields.
            for pr in _ctor_params(c, functions):
                crossable(pr.type, f"{c.name}({pr.name})")
            for m in c.methods:
                for pr in m.params:
                    crossable(pr.type, f"{c.name}.{m.name}({pr.name})")
                crossable(m.ret, f"{c.name}.{m.name}'s return")
        return cls(
            name=c.name, package=package, module=module,
            # RAW: the stubs carry the indentation the source had.
            doc=c.doc, decl=decl, is_value=c.is_value,
            produced=c.is_produced, constructs=c.constructs,
            wire_fields=tuple((f.name, m.ret.wire) for f, m in c.parts
                              if m.ret is not None),
            ctor=tuple(ParamModel.of(p, resolver)
                       for p in _ctor_params(c, functions)),
            # SURFACE only. A private method is bound because the tree
            # walk calls it, and nothing generated describes it.
            methods=tuple(MethodModel.of(m, resolver) for m in c.methods
                          if is_surface(m.name)),
        )

    @property
    def wire(self) -> str:
        """"proxy" unless the declaration proves the class is a value:
        stateful is the safe default on both sides."""
        return self.decl.wire or "proxy"

    @property
    def served(self) -> bool:
        return self.wire == "proxy"

    @property
    def wrapped(self) -> bool:
        """A hop onto a home thread, or a released GIL around a call
        that waits. A pool class that cannot block needs neither."""
        return self.decl.threading == "affine" or self.decl.blocking

    @property
    def qualified_module(self) -> str:
        return f"{self.package}.{self.module}"

    @property
    def service(self) -> str:
        return f"{self.name}{SERVICE}"

    @property
    def protocol_name(self) -> str:
        return f"{self.name}{PROTOCOL}"

    @property
    def async_name(self) -> str:
        return f"{ASYNC}{self.name}"

    @property
    def rpc_name(self) -> str:
        return f"{RPC}{self.name}"

    @property
    def message(self) -> str:
        return f"{self.name}Msg"

    def entry(self, final: bool = True) -> dict[str, Any]:
        """The manifest view of this class, key for key.

        `final=False` is the shape the generator consumes: no proto
        message name yet, and the round-trip helpers a value carries."""
        decl = self.decl
        wire_names: dict[str, Any] = ({
            "service": self.service,
            "acquire": {
                "path": f"/{PROTO_PACKAGE}.{self.service}/{ACQUIRE}",
                "req": f"{self.name}_{ACQUIRE}Req",
            },
            "protocol": self.protocol_name,
            "async_class": self.async_name,
            "rpc_class": self.rpc_name,
        } if self.served else {
            "message": self.message if final else None,
        })
        return {
            "name": self.name,
            "module": self.qualified_module,
            "doc": self.doc,
            # Empty for a produced value: it binds no C++ type.
            "binds": "" if self.is_value else "C" + self.name,
            "bases": ([f"{self.qualified_module}.{decl.base}"]
                      if decl.base else []),
            "threading": decl.threading,
            "abstract": decl.abstract,
            "constructs": self.constructs,
            "produced": self.produced,
            "wire": self.wire,
            "wire_fields": [list(f) for f in self.wire_fields],
            **({"unit": True} if decl.unit else {}),
            **({"tree": copy.deepcopy(decl.tree)} if decl.tree else {}),
            "blocking": decl.blocking,
            "wrapped": self.wrapped,
            "dunders": dunders(decl),
            "ctor": [p.entry() for p in self.ctor],
            "methods": [m.entry() for m in self.methods],
            "async_base": None,
            **wire_names,
            **({} if final else {
                "_helpers": sorted(("_from_parts", "_parts"))
                if decl.wire == "value" else [],
            }),
        }


def words_entry(cls: Class, package: str, module: str) -> dict[str, Any]:
    """One vocabulary, as the manifest carries it."""
    return {"name": cls.name, "module": f"{package}.{module}",
            "values": [m.value for m in cls.members],
            "doc": inspect.cleandoc(cls.doc)}
