"""The typed model every generator stage reads (huggorm#29).

The reader hands back structured types: `read.Type` knows its origin,
its arguments and its leaf. This module keeps that structure and adds
what a type IS - a scalar, a vocabulary, a union, an error, a value or
a proxy - resolved ONCE, against the declaration set. A name nothing
declares is refused here, when the model is built, instead of in an
emitter that would have guessed.

Each rule a stage needs is a property here, stated once.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from huggorm_dsl.declare import Decl
from huggorm_dsl.read import Class, Method, Module, Param, Type, is_surface
from huggorm_gen.payload import callspec
from huggorm_gen.payload.wiretypes import SCALAR_NAMES, SPELLED

# The words a proxy's RPC surface is spelled with. Every name below is
# the class name plus one of these.
PROTO_PACKAGE = "huggorm.v1"
SERVICE = "Service"
# Construction is an rpc on the class's OWN service, not a string-keyed
# call on Session. Session/Acquire took a class name and no arguments,
# so it could only ever build things whose constructor takes nothing -
# and it type-checked neither the name nor the absent arguments.
ACQUIRE = "Acquire"
PROTOCOL = "Like"
ASYNC = "Async"
RPC = "RPC"
# Free functions have no instance, so they cannot hang off a class's
# service. They share one.
FREE_SERVICE = "Functions"


def _camel(method: str) -> str:
    """`add_to_store` -> `AddToStore`.

    A message name, not a method name. The rpcs keep the binding's own
    snake_case on purpose - they are the Python surface spelled once
    - while a message is a TYPE, and protobuf types are PascalCase.

    One helper because the two used to disagree: the request kept the
    snake_case and the response camel-cased it, so one method had two
    spellings in one schema."""
    return method.title().replace("_", "")


def service_name(owner: str) -> str:
    return f"{owner}{SERVICE}"


def req_name(owner: str, method: str) -> str:
    return f"{owner}_{_camel(method)}Req"


def resp_name(owner: str, method: str) -> str:
    # Class-prefixed: LocalStore and RemoteStore share method names, and
    # top-level message names must be unique across the file.
    return f"{owner}_{_camel(method)}Resp"


def wire_method(method: str) -> str:
    """A method's name in the schema. A declared dunder such as
    `__call__` crosses as `call`: a protobuf identifier starts with a
    letter. Every Python surface keeps the dunder (huggorm#88)."""
    if method.startswith("__") and method.endswith("__"):
        return method.strip("_")
    return method


def method_path(owner: str, method: str) -> str:
    return f"/{PROTO_PACKAGE}.{service_name(owner)}/{wire_method(method)}"


@dataclass(frozen=True)
class RpcNames:
    """What one call is named on the wire. `owner` is a class, or
    `FREE_SERVICE` for a free function."""

    path: str
    req: str
    resp: str

    @classmethod
    def of(cls, owner: str, method: str) -> RpcNames:
        return cls(method_path(owner, method), req_name(owner, method),
                   resp_name(owner, method))

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
        """What a builtin or a declared name is."""
        if name in SCALAR_NAMES:
            return "scalar"
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
    # The wire scalar a leaf's C++ width needs: "uint" for a uint64_t.
    # Python has one int; only the crossing has two (huggorm#79).
    width: str = ""

    @property
    def optional(self) -> bool:
        return self.origin == "optional"

    @property
    def scalar(self) -> str | None:
        """The builtin this leaf goes in a field as, or None when it
        crosses as anything else."""
        if self.width:
            return self.width
        if self.kind == "scalar":
            return self.name
        return SPELLED.get(self.name)

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

    @property
    def container(self) -> bool:
        return self.origin in ("list", "dict")

    # Composing constructors, spelled the way the reader spells: a test
    # builds a shape the corpus does not declare without parsing text.
    @classmethod
    def named(cls, name: str, kind: Kind) -> TypeRef:
        return cls(name, "", (), kind, name)

    @classmethod
    def list_of(cls, t: TypeRef) -> TypeRef:
        return cls(f"list[{t.spelling}]", "list", (t,), t.kind, t.name)

    @classmethod
    def dict_of(cls, t: TypeRef) -> TypeRef:
        return cls(f"dict[str, {t.spelling}]", "dict", (t,), t.kind, t.name)

    @classmethod
    def optional_of(cls, t: TypeRef) -> TypeRef:
        return cls(f"{t.spelling} | None", "optional", (t,), t.kind, t.name)


# Why an opaque Python object never crosses. A DECISION, unlike every
# other reason below, which is a gap a later change could close.
NOT_DATA = (
    "an arbitrary Python object is not data - here it is a "
    "callable the binding keeps and calls back. A remote client "
    "registering one would make the evaluator call BACK over the "
    "socket, on its own evaluation thread, once per invocation - "
    "a distributed call in a hot loop. In-process only, by "
    "decision rather than omission (huggorm#33).")


def wire_blocker(t: TypeRef, served: frozenset[str]) -> str | None:
    """Why this type cannot cross the wire, or None if it can.

    Reported rather than raised: a method that cannot cross keeps its
    in-process wrapper, and the build says exactly what is missing.
    The reader has already refused what no binding carries - a set, a
    non-str map key, a bare container - so only what a protobuf field
    cannot hold is left to say."""
    if t.optional:
        inner = t.required
        # A repeated field has no presence, and needs none.
        if inner.container:
            return (f"{t.spelling}: a repeated field has no presence and "
                    f"needs none - an absent container IS an empty one. "
                    f"Declare {inner.spelling} and return it empty.")
        t = inner
    if t.container:
        element = t.args[0]
        if element.container:
            return (f"{t.spelling}: proto3 cannot put a {element.origin} "
                    f"inside a map. A nested attribute set needs the "
                    f"recursive value message (huggorm#30)"
                    if t.origin == "dict" else
                    f"{t.spelling}: proto3 cannot repeat a {element.origin}. "
                    f"A list of them needs the recursive value message "
                    f"(huggorm#30)")
        if element.optional:
            return (f"{t.spelling}: an element of a map or a repeated field "
                    f"has no presence, so {element.spelling} cannot say "
                    f"None there")
        if element.kind == "proxy":
            # One lease per element, and nothing grants leases in bulk.
            return (f"{t.spelling}: a container of proxies would grant one "
                    f"lease per element, and nothing grants leases in bulk "
                    f"(huggorm#31)")
        t = element
    if t.kind == "opaque":
        return NOT_DATA
    if t.kind == "proxy" and t.name not in served:
        # A handle only some service can answer is worth sending.
        return (f"{t.spelling} is a proxy with no service: it crosses as a "
                f"handle, and nothing is wrapped to answer a call on that "
                f"handle. A remote caller would receive an id it cannot "
                f"use.")
    if t.kind == "module" and t.name not in SPELLED:
        return (f"{t.spelling} has no wire spelling: a type from another "
                f"module crosses only as the builtin wiretypes.SPELLED "
                f"names for it, and SPELLED names none for this one")
    return None


def type_ref(t: Type, resolver: Resolver) -> TypeRef:
    # The vocabulary already says the Python half: `Bint` is
    # `Annotated[bool, Cxx("bint")]`, so `python` is `bool`.
    name = t.leaf.python
    return TypeRef(
        spelling=t.python,
        origin=t.origin,
        args=tuple(type_ref(a, resolver) for a in t.args),
        # `pathlib.Path`, `datetime.timedelta`: a type in a module,
        # imported as itself.
        kind="module" if t.leaf.module else resolver.kind(name),
        name=name,
        width=t.cxx.width if t.cxx is not None and not t.origin else "",
    )


@dataclass(frozen=True)
class FieldModel:
    """One part a wire value or an error is rebuilt from. Parts are in
    constructor order: the far side calls `cls(*parts)`."""

    name: str
    type: TypeRef

    @classmethod
    def of(cls, name: str, t: Type, resolver: Resolver) -> FieldModel:
        crossable(t, name)
        return cls(name, type_ref(t, resolver))


def crossable(t: Type | None, where: str) -> None:
    """Refuse a container of a C++ width.

    A leaf carries its width to the schema and the codec. A container
    of one does not: the width reaches the reader on the container,
    and naming the container `uint` would be wrong (huggorm#79)."""
    held = t.required if t is not None else None
    if held is not None and held.origin and held.leaf.cxx is not None \
            and held.leaf.cxx.width:
        raise TypeError(
            f"{where}: '{held.python}' holds a {held.leaf.cxx.spelling}, and "
            f"a container of a width has no wire spelling yet. See "
            f"huggorm#79.")


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
    # The vocabulary a member default names, which a module writing the
    # default has to import, or "".
    default_class: str = ""

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
        return cls(p.name, type_ref(declared, resolver), default,
                   p.type.python if p.member else "")


def blockers(params: Sequence[ParamModel], returns: TypeRef | None,
             served: frozenset[str]) -> list[str]:
    """Why a call has no rpc, or [] when it has one."""
    out = [f"parameter {p.name!r}: {why}" for p in params
           if (why := wire_blocker(p.type, served))]
    if returns is not None and (why := wire_blocker(returns, served)):
        out.append(f"return type: {why}")
    return out


@dataclass(frozen=True)
class MethodModel:
    name: str
    params: tuple[ParamModel, ...]
    returns: TypeRef | None
    doc: str
    # Declared `@blocks`: the binding releases the GIL for it.
    blocks: bool = False

    @classmethod
    def of(cls, m: Method, resolver: Resolver) -> MethodModel:
        return cls(m.name,
                   tuple(ParamModel.of(p, resolver) for p in m.params),
                   type_ref(m.ret, resolver) if m.ret is not None else None,
                   _clean(m.doc), m.blocks)

    @property
    def return_spelling(self) -> str:
        return self.returns.spelling if self.returns is not None else "None"

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

    @property
    def rpc(self) -> RpcNames:
        return RpcNames.of(FREE_SERVICE, self.name)

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
    wire_fields: tuple[FieldModel, ...]
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
            wire_fields=tuple(FieldModel.of(f.name, m.ret, resolver)
                              for f, m in c.parts if m.ret is not None),
            ctor=tuple(ParamModel.of(p, resolver)
                       for p in _ctor_params(c, functions)),
            # SURFACE only. A private method is bound because the tree
            # walk calls it, and nothing generated describes it.
            methods=tuple(MethodModel.of(m, resolver) for m in c.methods
                          if is_surface(m.name)),
        )

    def method(self, name: str) -> MethodModel:
        return next(m for m in self.methods if m.name == name)

    @property
    def tree(self) -> callspec.Tree | None:
        """How a value that holds values is walked, or None.

        The declaration states it as `@tree(...)` keywords. A list has
        `item` and an attribute set has `name` and `value`; `value`
        reads the child in both, so the walker has one shape."""
        spec = self.decl.tree
        if not spec:
            return None

        def walk(how: Mapping[str, str]) -> callspec.Walk:
            return callspec.Walk(how["size"], how.get("value") or how["item"],
                                 how.get("name", ""))

        return callspec.Tree(spec["kind"], spec.get("identity", ""),
                             {k: tuple(v) for k, v in spec["scalars"].items()},
                             walk(spec["list"]), walk(spec["attrs"]))

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
    def execution(self) -> str:
        """Where a call runs. Served is addressability and WRAPPED is
        execution: an unwrapped class cannot wait, so its calls run
        inline - a hop buys nothing, and a request would push a
        "finalized" marker from a pool thread into the process queue
        for a call no reader made."""
        return self.decl.threading if self.wrapped else "inline"

    @property
    def qualified_module(self) -> str:
        return f"{self.package}.{self.module}"

    @property
    def binds(self) -> str:
        """The C++ class name the binding defines, or "" for a produced
        value, which binds no C++ type."""
        return "" if self.is_value else f"C{self.name}"

    @property
    def service(self) -> str:
        return service_name(self.name)

    @property
    def acquire(self) -> RpcNames:
        """The rpc that constructs one of these remotely."""
        return RpcNames.of(self.name, ACQUIRE)

    def rpc(self, m: MethodModel) -> RpcNames:
        return RpcNames.of(self.name, m.name)

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

@dataclass(frozen=True)
class EnumModel:
    """One string vocabulary. A member is a str, so it crosses as one."""

    name: str
    module: str
    values: tuple[str, ...]
    doc: str

    @classmethod
    def of(cls, c: Class, package: str, module: str) -> EnumModel:
        return cls(c.name, f"{package}.{module}",
                   tuple(m.value for m in c.members), _clean(c.doc))

@dataclass(frozen=True)
class ErrorModel:
    """One declared exception class. It crosses as its name and its
    wire fields, which it inherits through the MRO."""

    name: str
    # Bases declared in the same errors document, not `Exception`.
    bases: tuple[str, ...]
    wire_fields: tuple[FieldModel, ...]

@dataclass(frozen=True)
class Errors:
    """The exception surface: the module the hierarchy is emitted into
    ("" when nothing declares one) and its classes, by name."""

    module: str
    classes: Mapping[str, ErrorModel]

# Why a free function with no threading policy has no rpc.
NO_POLICY = ("no threading policy, so the function has no async form for "
             "a server to call")


@dataclass(frozen=True)
class Model:
    """The whole declaration set, resolved: what every stage reads.

    Declared order throughout, because a later pass numbers protobuf
    fields from it and a reorder is a wire change."""

    classes: Mapping[str, ClassModel]
    functions: Mapping[str, FunctionModel]
    # Every union alias, to its arms in declared order.
    unions: Mapping[str, tuple[TypeRef, ...]]
    # The classes that are HANDED BACK rather than constructed.
    returned: frozenset[str]
    # A type the async surface spells differently: `pathlib.Path` is
    # `anyio.Path` there - same value, awaitable methods.
    twins: Mapping[str, str]
    enums: Mapping[str, EnumModel]
    errors: Errors

    @property
    def blocking_unwrapped(self) -> list[tuple[ClassModel, MethodModel]]:
        """Every `@blocks` method on a class with no async form. An
        async caller calls it synchronously and stalls its event loop
        (huggorm#25)."""
        return [(c, m) for c in self.classes.values() if not c.wrapped
                for m in c.methods if m.blocks]

    @property
    def served(self) -> frozenset[str]:
        """Every class with a service behind its handles: every proxy."""
        return frozenset(n for n, c in self.classes.items() if c.served)

    @property
    def constructed(self) -> list[ClassModel]:
        """Every class a caller builds, by name."""
        return [self.classes[n] for n in sorted(self.classes)
                if n not in self.returned]

    @property
    def handed_back(self) -> list[ClassModel]:
        """Every class only a call hands back, by name."""
        return [self.classes[n] for n in sorted(self.classes)
                if n in self.returned]

    @property
    def acquirable(self) -> list[ClassModel]:
        """Every served class a remote caller constructs, by name. A
        class only a call hands back has no Acquire: there is nothing
        to build one from."""
        return [c for c in self.constructed if c.served]

    @property
    def ordered_served(self) -> list[ClassModel]:
        """Every served class: the returned ones, then the constructed
        ones, each by name - the order every surface emits them in."""
        return [c for c in (*self.handed_back, *self.constructed)
                if c.served]

    def offered(self, m: MethodModel) -> bool:
        """Whether a method crosses the wire, which is also whether the
        protocol may promise it: both implementations must offer it."""
        return not blockers(m.params, m.returns, self.served)

    def function_blockers(self, fn: FunctionModel) -> list[str]:
        """Why a free function has no rpc, or [] when it has one."""
        if not fn.wrapped:
            return [NO_POLICY]
        return blockers(fn.params, fn.returns, self.served)


def returned_names(module_classes: Sequence[tuple[Mapping[str, Class],
                                                  Class]]) -> frozenset[str]:
    """The classes some declared method hands back and nothing builds.

    Every name a return type holds counts: `list[StorePath]` hands back
    StorePaths as surely as `StorePath` does. A class a caller can
    construct is an entry point that happens to be returned, so it is
    not one; `@produced(by=...)` with no `__init__` is the whole test."""
    out: set[str] = set()
    for known, cls in module_classes:
        for m in cls.methods:
            if m.ret is None:
                continue
            ret = m.ret.required
            if ret.origin == "list":
                ret = ret.element
            name = ret.python
            if ret.origin or name not in known or known[name].is_words:
                continue
            held = known[name]
            if held.decl.built_by and held.ctor is None:
                out.add(name)
    return frozenset(out)

