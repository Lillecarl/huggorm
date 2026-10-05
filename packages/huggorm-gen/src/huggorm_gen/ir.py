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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Literal

from huggorm_dsl.declare import Decl
from huggorm_dsl.read import Class, Method, Module, Param, Type, is_surface
from huggorm_gen import cxx
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
    # How nanobind spells it by value, and the caster that needs.
    # Empty on a hand-built shape.
    cxx: str = ""
    caster: str | None = None

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
    spelled, caster = cxx.value(t, resolver.known)
    return TypeRef(
        spelling=t.python,
        origin=t.origin,
        args=tuple(type_ref(a, resolver) for a in t.args),
        # `pathlib.Path`, `datetime.timedelta`: a type in a module,
        # imported as itself.
        kind="module" if t.leaf.module else resolver.kind(name),
        name=name,
        width=t.cxx.width if t.cxx is not None and not t.origin else "",
        cxx=spelled, caster=caster,
    )


@dataclass(frozen=True)
class FieldModel:
    """One part a wire value or an error is rebuilt from. Parts are in
    constructor order: the far side calls `cls(*parts)`."""

    name: str
    type: TypeRef
    # The accessor that reads the part, when it is not `name`.
    accessor: str = ""
    # The C++ set the part is rebuilt into, or "" when a vector is the
    # member: `@reads(collection=...)`, else the element class's.
    collection: str = ""

    @property
    def read(self) -> str:
        """The accessor that reads the part."""
        return self.accessor or self.name

    @classmethod
    def of(cls, name: str, t: Type, resolver: Resolver, accessor: str = "",
           collection: str = "") -> FieldModel:
        crossable(t, name)
        return cls(name, type_ref(t, resolver),
                   "" if accessor == name else accessor, collection)


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


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


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
    # The default as C++ spells it, or "" when there is none.
    cxx_default: str = ""
    # How nanobind spells the parameter, and the caster that needs. A
    # fact about the parameter, not the type: a by-value type is often
    # passed by reference.
    cxx: str = ""
    caster: str | None = None
    # A container whose absence is spelled None: it arrives as an
    # optional and the binding opens it to an empty container.
    absent: bool = False
    # The C++ call that parses a vocabulary's string, or "".
    parsed_by: str = ""
    # The C++ collection a `list[T]` becomes, or "" for a vector.
    collection: str = ""
    # The member a HANDLE parameter is unwrapped through, or "".
    via: str = ""

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
        spelled, caster = cxx.param(p.type, resolver.known)
        handle = cxx.handle(p.type, resolver.known)
        return cls(p.name, type_ref(declared, resolver), default,
                   p.type.python if p.member else "", cxx.default(p),
                   spelled, caster, absent=cxx.absent(p),
                   parsed_by=cxx.parsed_by(p.type, resolver.known),
                   collection=cxx.collection(p.type, resolver.known),
                   via=handle.decl.via if handle is not None else "")


def blockers(params: Sequence[ParamModel], returns: TypeRef | None,
             served: frozenset[str]) -> list[str]:
    """Why a call has no rpc, or [] when it has one."""
    out = [f"parameter {p.name!r}: {why}" for p in params
           if (why := wire_blocker(p.type, served))]
    if returns is not None and (why := wire_blocker(returns, served)):
        out.append(f"return type: {why}")
    return out


@dataclass(frozen=True)
class ArmTest:
    """The test that a tagged union holds one arm: `hold->ask == tag`."""

    hold: str
    ask: str
    tag: str


@dataclass(frozen=True)
class Tagged:
    """`@tagged`: how a handle reaches the tagged union it wraps, how it
    asks which arm the union holds, and each arm as (word, enumerator),
    in declared order."""

    hold: str
    ask: str
    arms: tuple[tuple[str, str], ...]

    @classmethod
    def of(cls, decl: Decl) -> Tagged | None:
        if decl.tagged is None:
            return None
        hold, ask, table = decl.tagged
        return cls(hold, ask, tuple(table.items()))

    def test(self, word: str, where: str) -> ArmTest:
        tag = dict(self.arms).get(word)
        if tag is None:
            raise ValueError(
                f"{where} names no arm; @tagged offers "
                f"{sorted(w for w, _ in self.arms)}")
        return ArmTest(self.hold, self.ask, tag)


@dataclass(frozen=True)
class Fill:
    """`@fills(maker, arm)`: the first parameter, `target`, is a builder
    `maker` made for that arm."""

    maker: str
    target: str
    arm: ArmTest


def _spells(m: Method, where: str, resolver: Resolver) -> tuple[str, ...]:
    """`@spells`, each name checked to BE an enum-backed vocabulary.

    The decorator takes a string because a declaration holds constants,
    so nothing else catches a typo - and a skipped name fails much
    later, as a missing `huggorm::as_word` overload in the C++."""
    for name in m.spells:
        held = resolver.known.get(name)
        if held is None or not (held.is_words and held.decl.enumerated):
            raise TypeError(
                f"{where}: @spells({name!r}) names no "
                f"enum-backed vocabulary this declaration can see. "
                f"Import the declaration that declares it.")
    return m.spells


def _attribute(owner: Class, m: Method) -> TypeError:
    """The refusal a `@property` accessor gets, and why it is one.

    `@property` says an accessor is an ATTRIBUTE rather than a call.
    No stage honours that, and four would have to:

    - `nbemit`, which would bind `def_prop_ro` instead of `def`;
    - `nbemit._identity_semantics`, which writes `h.attr("nar_size")()`
      into `__repr__`, `__hash__` and `_parts` - a call, on every part;
    - `pyi.py`, which emits `def nar_size(self) -> int` in the stub;
    - `wire.py` and the generated wrappers, which read a part the way
      `_parts` does.

    So a binding that honoured the word alone would disagree with its
    own stub and drop the value off the wire (huggorm#76)."""
    return TypeError(
        f"{owner.name}.{m.name}: @property makes this accessor an "
        f"ATTRIBUTE, and every reader of this class calls it - the "
        f"emitted `_parts`, the stub and the wire all spell "
        f"`obj.{m.name}()`. Drop the @property and declare a plain "
        f"accessor, or teach all four (huggorm#76).")


def _tagged(owner: Class, m: Method) -> Tagged | None:
    """The owner's union, for a method that `@guard`s or `@names` it."""
    if not (m.guard or m.names):
        return None
    tagged = Tagged.of(owner.decl)
    if tagged is None:
        raise ValueError(
            f"{owner.name}.{m.name}: needs @tagged(reach, ask, ...) on the "
            f"class to say how to reach the union, how to ask which arm "
            f"it holds, and what the arms are called")
    return tagged


def _fill(owner: Class, m: Method, params: Sequence[ParamModel],
          resolver: Resolver) -> Fill | None:
    """A `@fills` method's target check. The TARGET's class holds the
    arm table, not the owner's: `list_append` is declared on the
    evaluator and fills a Value."""
    if m.fills is None:
        return None
    where = f"{owner.name}.{m.name}"
    maker, arm = m.fills
    if not params:
        raise ValueError(f"{where}: @fills needs a target")
    held = resolver.known.get(params[0].type.spelling)
    tagged = Tagged.of(held.decl) if held is not None else None
    if tagged is None:
        raise ValueError(
            f"{where}: @fills needs its target's class to "
            f"carry @tagged, to check the arm being filled")
    return Fill(maker, params[0].name,
                tagged.test(arm, f'{where}: @fills(..., "{arm}")'))


@dataclass(frozen=True)
class MethodModel:
    name: str
    params: tuple[ParamModel, ...]
    returns: TypeRef | None
    doc: str
    # Declared `@blocks`: the binding releases the GIL for it.
    blocks: bool = False
    # Declared `@instant`: the binding keeps the GIL on a blocking class.
    instant: bool = False
    # The C++ member function it binds, when it is not `name`.
    cxx_name: str = ""
    # The C++ it carries, verbatim, or "".
    cxx_body: str = ""
    # The data member it reads (`@reads`), or "".
    reads: str = ""
    # The arm it needs (`@guard`).
    guard: ArmTest | None = None
    # `@names`: it answers the name of the arm this union holds.
    names: Tagged | None = None
    # The initialiser a producer calls (`@produces`), or "".
    produces: str = ""
    fills: Fill | None = None
    # `@local`: bound on the object and kept off the wire.
    local: bool = False
    # The C++ type of the HANDLE class it returns, or "".
    returns_handle: str = ""
    # It returns a vocabulary with a C++ enum behind it.
    returns_word: bool = False
    # Headers its body needs (`@needs`), and the vocabularies its body
    # converts that no signature names (`@spells`).
    headers: tuple[str, ...] = ()
    spells: tuple[str, ...] = ()

    @classmethod
    def of(cls, m: Method, owner: Class, resolver: Resolver) -> MethodModel:
        handle = cxx.handle(m.ret, resolver.known)
        ret = m.ret.required if m.ret is not None else None
        word = (None if ret is None or ret.origin
                else resolver.known.get(ret.python))
        if m.prop:
            raise _attribute(owner, m)
        params = tuple(ParamModel.of(p, resolver) for p in m.params)
        tagged = _tagged(owner, m)
        where = f'{owner.name}.{m.name}: @guard("{m.guard}")'
        guard = (tagged.test(m.guard, where)
                 if tagged is not None and m.guard else None)
        return cls(m.name, params,
                   type_ref(m.ret, resolver) if m.ret is not None else None,
                   _clean(m.doc), m.blocks, instant=m.instant,
                   cxx_name=m.cxx_name, cxx_body=m.cxx_body, reads=m.reads,
                   guard=guard, names=tagged if m.names else None,
                   produces=m.produces,
                   fills=_fill(owner, m, params, resolver),
                   local=m.local,
                   returns_handle=cxx.held(handle) if handle else "",
                   returns_word=(word is not None and word.is_words
                                 and bool(word.decl.enumerated)),
                   headers=m.headers,
                   spells=_spells(m, f"{owner.name}.{m.name}", resolver))

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
    # The binding this calls, when it is not the function of the same
    # name: `Input.fingerprint` for `input_fingerprint`.
    calls: str = ""
    # The C++ it binds by name (`@binds`), or the body it carries.
    cxx_name: str = ""
    cxx_body: str = ""
    blocks: bool = False
    instant: bool = False
    headers: tuple[str, ...] = ()
    spells: tuple[str, ...] = ()
    # Runs once when the module is imported (`@startup`).
    startup: bool = False
    # Turns a library exception into a Python one (`@translator`).
    translator: bool = False

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
                   _clean(fn.doc), cxx_name=fn.binds, cxx_body=fn.cxx_body,
                   blocks=fn.blocks, instant=fn.instant, headers=fn.headers,
                   spells=_spells(fn, fn.name, resolver), startup=fn.startup,
                   translator=fn.translator)

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


def _factory(cls: Class, functions: Sequence[Method]) -> Method | None:
    """The free function that builds one of these, when this module
    declares it and the class has an `__init__` for it to stand in for."""
    if not cls.decl.factory or cls.ctor is None:
        return None
    return next((f for f in functions if f.name == cls.decl.factory), None)


def _ctor_params(cls: Class,
                 functions: Sequence[Method] = ()) -> tuple[Param, ...]:
    """The parameters a caller passes to build one of these.

    A class with a factory is built by it, so the factory's
    parameters - defaults included - are the signature."""
    made = _factory(cls, functions)
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
    # Every method the binding binds, private ones included: the tree
    # walk calls those. `methods` is the surface every other stage reads.
    bound: tuple[MethodModel, ...]
    # The declared `__init__`, and the factory that runs in its place.
    init: MethodModel | None = None
    factory: FunctionModel | None = None
    # A declared `_from_parts`, which a value carries when its C++ type
    # cannot be rebuilt from the parts as they are.
    from_parts: MethodModel | None = None
    # The header that declares its C++ type, or "".
    header: str = ""

    @classmethod
    def of(cls, c: Class, package: str, module: str, resolver: Resolver,
           functions: Sequence[Method] = ()) -> ClassModel:
        return _shaped(cls._of(c, package, module, resolver, functions))

    @classmethod
    def _of(cls, c: Class, package: str, module: str, resolver: Resolver,
            functions: Sequence[Method]) -> ClassModel:
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
            wire_fields=tuple(
                FieldModel.of(f.name, m.ret, resolver, f.read,
                              m.member_collection
                              or cxx.collection(m.ret, resolver.known))
                for f, m in c.parts if m.ret is not None),
            ctor=tuple(ParamModel.of(p, resolver)
                       for p in _ctor_params(c, functions)),
            bound=tuple(MethodModel.of(m, c, resolver) for m in c.methods),
            init=(MethodModel.of(c.ctor, c, resolver)
                  if c.ctor is not None else None),
            factory=(FunctionModel.of(made, package, module, resolver)
                     if (made := _factory(c, functions)) else None),
            from_parts=(MethodModel.of(c.from_parts, c, resolver)
                        if c.from_parts is not None else None),
            header=decl.header,
        )

    @property
    def methods(self) -> tuple[MethodModel, ...]:
        """The SURFACE. A private method is bound because the tree walk
        calls it, and nothing generated describes it."""
        return tuple(m for m in self.bound if is_surface(m.name))

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

def _shaped(cls: ClassModel) -> ClassModel:
    """`cls`, refused when its value shape cannot round-trip.

    Each check is one the binding depends on: `text=` renders through
    an accessor, and `_from_parts` rebuilds a value from its wire
    fields, in order."""
    decl = cls.decl
    if decl.text and not any(m.name == decl.text for m in cls.bound):
        raise TypeError(
            f"{cls.name}: \"{decl.text}\" names no accessor on this class.")
    fields = [f.name for f in cls.wire_fields]
    if (decl.wire == "value" and (fields or decl.unit) and not cls.is_value
            and cls.init is not None and len(cls.init.params) != len(fields)):
        # `_from_parts` IS the constructor here. A forgotten `@local`
        # breaks that: an accessor joins the wire by existing, `_parts`
        # grows a value, and the constructor does not.
        raise TypeError(
            f"{cls.name}: `_from_parts` is the constructor, which "
            f"takes {len(cls.init.params)} parameter(s), and "
            f"{len(fields)} accessor(s) cross the wire: "
            f"{fields}. An accessor joins the "
            f"wire by existing - mark the ones that should not "
            f"@local, or give the constructor what they send.")
    if not cls.is_value:
        return cls
    if cls.from_parts is not None:
        if not cls.from_parts.cxx_body:
            raise TypeError(
                f"{cls.name}: declares `_from_parts` with no body. Write "
                f"one, or drop the declaration and let the aggregate "
                f"build it.")
        return cls
    members = [m.name for m in cls.bound
               if m.returns is not None and not m.local]
    if (any(f.read != f.name for f in cls.wire_fields)
            and fields != members):
        # A part read through another accessor arrives as that
        # accessor's type, and `_from_parts` initialises the record
        # POSITIONALLY: a part out of member order lands in the wrong
        # member.
        raise TypeError(
            f"{cls.name}: its parts {fields} are not its members "
            f"{members}, in order.")
    return cls


@dataclass(frozen=True)
class UnionModel:
    """One sum type. Each arm is named as the wire names it, a scalar
    by its wire spelling, and carries the C++ the arms' variant holds
    it as - the bare type, never a holder - with the caster it needs."""

    name: str
    decl: Decl
    arms: tuple[TypeRef, ...]
    header: str = ""

    @classmethod
    def of(cls, u: Class, resolver: Resolver) -> UnionModel:
        arms = []
        for a in u.decl.arms:
            held = u.decl.scalars.get(a) or Type(python=a, bound=True)
            _, caster = cxx.value(held, resolver.known)
            arms.append(replace(TypeRef.named(a, resolver.kind(a)),
                                cxx=cxx.arm(u, a, resolver.known),
                                caster=caster))
        return cls(u.name, u.decl, tuple(arms), u.decl.header)


@dataclass(frozen=True)
class WordModel:
    """One vocabulary member: its Python name and the word it is."""

    name: str
    value: str


@dataclass(frozen=True)
class EnumModel:
    """One string vocabulary. A member is a str, so it crosses as one."""

    name: str
    module: str
    members: tuple[WordModel, ...]
    doc: str
    decl: Decl
    # The header that declares its C++ enum, or "".
    header: str = ""

    @classmethod
    def of(cls, c: Class, package: str, module: str) -> EnumModel:
        return cls(c.name, f"{package}.{module}",
                   tuple(WordModel(m.name, m.value) for m in c.members),
                   _clean(c.doc), c.decl, c.decl.header)

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
class ModuleModel:
    """One declaration file, as one translation unit sees it."""

    name: str
    # RAW, as the file wrote it.
    doc: str
    # Every class it declares, in declared order.
    classes: tuple[ClassModel, ...]
    # Every function it declares: startup hooks, translators and
    # factories too, which `Model.functions` leaves out.
    functions: tuple[FunctionModel, ...]
    # The ones a caller imports: `Module.exported`.
    exported: tuple[FunctionModel, ...]
    # Every declared name the unit can resolve, its own and its
    # imports'. The model behind one is `Model.declared`.
    visible: frozenset[str]

    @classmethod
    def of(cls, mod: Module, package: str) -> ModuleModel:
        resolver = Resolver.of(mod)
        functions = {fn.name: FunctionModel.of(fn, package, mod.name, resolver)
                     for fn in mod.functions}
        return cls(
            mod.name, mod.doc,
            tuple(ClassModel.of(c, package, mod.name, resolver, mod.functions)
                  for c in mod.classes),
            tuple(functions.values()),
            tuple(functions[fn.name] for fn in mod.exported),
            frozenset(mod.known))

    def bindable(self) -> tuple[ClassModel, ...]:
        """The classes nanobind binds: a C++ type, or a record the
        emitter declares.

        A vocabulary has no C++ object: it crosses as the string its
        member already is. A produced value with no `@binding(cxx=...)`
        has no C++ type either, until the emitter declares its struct.
        `generate.emit_module` prints what this leaves out."""
        return tuple(c for c in self.classes if c.decl.cxx or c.is_value)

    @property
    def startup(self) -> tuple[FunctionModel, ...]:
        return tuple(fn for fn in self.functions if fn.startup)

    @property
    def translators(self) -> tuple[FunctionModel, ...]:
        return tuple(fn for fn in self.functions if fn.translator)


@dataclass(frozen=True)
class Model:
    """The whole declaration set, resolved: what every stage reads.

    Declared order throughout, because a later pass numbers protobuf
    fields from it and a reorder is a wire change."""

    classes: Mapping[str, ClassModel]
    functions: Mapping[str, FunctionModel]
    # Every union alias, its arms in declared order.
    unions: Mapping[str, UnionModel]
    # The classes that are HANDED BACK rather than constructed.
    returned: frozenset[str]
    # A type the async surface spells differently: `pathlib.Path` is
    # `anyio.Path` there - same value, awaitable methods.
    twins: Mapping[str, str]
    enums: Mapping[str, EnumModel]
    errors: Errors
    # Each declaration file. Not a name table: a module name is not
    # a name a caller imports.
    modules: tuple[ModuleModel, ...] = ()

    def __post_init__(self) -> None:
        # A produced class has no way in but a call that returns it,
        # and its refusing `__init__` names those calls.
        if missing := sorted(n for n, c in self.classes.items()
                             if c.decl.produced and n not in self.producers):
            raise TypeError(
                f"{', '.join(missing)}: declared @produced, and no declared "
                f"call returns one. Declare the call that makes it.")

    def module(self, name: str) -> ModuleModel:
        return next(m for m in self.modules if m.name == name)

    def declared(self, t: TypeRef
                 ) -> ClassModel | UnionModel | EnumModel | None:
        """The model behind one leaf, or None for a builtin, a module
        type or an exception, which carry no C++ of their own here.

        Keyed by the leaf's resolved KIND, so a name is never read as
        the wrong sort of declaration."""
        if t.origin:
            return None
        if t.kind in ("value", "proxy"):
            return self.classes[t.name]
        if t.kind == "union":
            return self.unions[t.name]
        if t.kind == "enum":
            return self.enums[t.name]
        return None

    @cached_property
    def producers(self) -> dict[str, tuple[str, ...]]:
        """Each call that hands back a class, by the class's name,
        sorted. A set's question: `Store.query_path_info` makes a
        `PathInfo`, and `pathinfo.py` does not import `Store`.

        A call that returns `X`, `X | None` or `list[X]` makes an `X`,
        read off the return types so no declaration names it twice."""
        def made(t: TypeRef | None) -> str | None:
            if t is None:
                return None
            t = t.required
            if t.origin == "list":
                t = t.args[0]
            return None if t.origin else t.name

        out: dict[str, list[str]] = {}
        for unit in self.modules:
            for c in unit.classes:
                for m in c.bound:
                    if (name := made(m.returns)) is not None:
                        out.setdefault(name, []).append(f"{c.name}.{m.name}")
            for fn in unit.functions:
                if (name := made(fn.returns)) is not None:
                    out.setdefault(name, []).append(fn.name)
        return {name: tuple(sorted(calls)) for name, calls in out.items()}

    @property
    def blocking_methods(self) -> list[FunctionModel]:
        """The async form of each `@blocks` method on a value: a free
        coroutine, `input_fingerprint(input, store)`.

        A value has no wrapper (huggorm#17), so its methods run on the
        caller's thread. That is right for a read and wrong for a call
        that waits, so a method that blocks gets the pool hop a free
        function gets (huggorm#25)."""
        out = []
        for c in self.classes.values():
            for m in c.methods:
                if not m.blocks or c.wrapped:
                    continue
                if c.served:
                    raise TypeError(
                        f"{c.name}.{m.name} is @blocks, and {c.name} is "
                        f"served but not wrapped, so its async form calls it "
                        f"on the event loop. Declare {c.name} "
                        f"@binding(blocking=True).")
                name = f"{_snake(c.name)}_{m.name}"
                if name in self.functions:
                    raise TypeError(
                        f"{c.name}.{m.name}: its async form is named {name}, "
                        f"and a declared free function has that name.")
                me = TypeRef(c.name, "", (), "value", c.name)
                out.append(FunctionModel(
                    name, c.qualified_module, c.decl.threading,
                    (ParamModel(_snake(c.name), me, None), *m.params),
                    m.returns, m.doc, calls=f"{c.name}.{m.name}"))
        return out

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
    not one; `@produced` with no `__init__` is the whole test."""
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
            if held.decl.produced and held.ctor is None:
                out.add(name)
    return frozenset(out)

