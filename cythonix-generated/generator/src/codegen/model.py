"""
Introspect binding classes into plain protocol dicts.

No I/O, no ast — pure reflection plus the parsed pxd surface handed in
by the caller. The dict shape is the contract between the sources
(the bindings pxd + the installed bindings) and the emitter (emitter.py).
"""

import ast
import contextlib
import importlib
import inspect
from enum import Enum
from types import ModuleType
from typing import Any, get_args, get_origin, get_type_hints

from codegen.wiretypes import (
    CONTAINERS,
    head,
    list_value,
    map_value,
    names_in,
)

# One class or function, reflected into the plain dict every layer
# above reads. Named rather than spelled dict[str, Any] everywhere:
# it is the contract between the sources and the emitter, and the one
# place to tighten if it becomes a TypedDict (tasks/029).
Proto = dict[str, Any]
Api = dict[str, Any]

_PRIMITIVES = {
    "string": "str",
    # Real Nix returns views into an object's own storage. A binding
    # copies before anything reaches Python - a view outliving its
    # owner is a dangling pointer, not an exception - so by the time a
    # type reaches this table it is a str (tasks/015).
    "string_view": "str",
    "int": "int",
    "long": "int",
    "long long": "int",
    "size_t": "int",
    "ssize_t": "int",
    # <stdint.h> spellings. c_eval.pxd already declares int64_t; without
    # these the pxd return type went unmapped and the backfill fell back
    # to whatever the live annotation happened to say.
    "int8_t": "int",
    "int16_t": "int",
    "int32_t": "int",
    "int64_t": "int",
    "uint8_t": "int",
    "uint16_t": "int",
    "uint32_t": "int",
    "uint64_t": "int",
    "double": "float",
    "float": "float",
    "bool": "bool",
    "bint": "bool",
    "void": "None",
}

# Live Cython annotations can carry C-only type names verbatim (Cython
# stores the written annotation string). Normalize them here, once, so
# everything downstream - emitter imports, gRPC schema, remote codec -
# sees plain Python scalars.
_C_ALIASES = {"bint": "bool"}


def _normalize(t: str) -> str:
    return _C_ALIASES.get(t, t)


def binding_map(bindings_module: ModuleType) -> dict[str, str]:
    """pxd declaration name -> Python binding name, from the `_binds`
    each cdef class declares.

    This is the ONLY link between the two hand-written files. It used to
    be a naming convention - strip a leading "C" and hope the bindings
    module has the rest - living in this module, invisible from either
    file it constrained. A pxd class named anything else silently failed
    to map, and the failure was swallowed on returns and fatal on
    parameters, for no reason.

    Read from __dict__, not getattr: LocalStore would otherwise inherit
    Store's _binds and claim to be CStore."""
    out: dict[str, str] = {}
    for name in sorted(dir(bindings_module)):
        obj = getattr(bindings_module, name)
        if not isinstance(obj, type):
            continue
        c_name = obj.__dict__.get("_binds")
        if c_name is None:
            continue
        if c_name in out:
            raise ValueError(
                f"{name} and {out[c_name]} both declare _binds = {c_name!r}")
        out[c_name] = name
    return out


def check_binding_map(api: Api, mapping: dict[str, str]) -> list[str]:
    """Complaints about the pxd and the bindings disagreeing.

    Claiming a class the pxd does not declare is always a mistake - a
    typo, or a declaration that was renamed on one side only. Fatal.

    The other direction, a pxd class nothing binds, is reported as a
    warning by the caller: declaring a C++ type only to name it in a
    signature is legitimate, and real Nix headers will be full of them.
    Here it is a coverage hole worth seeing."""
    declared = set(api["classes"])
    bad = [
        f"{py}._binds = {c!r}: no class of that name in the pxd "
        f"(declared: {sorted(declared)})"
        for c, py in sorted(mapping.items())
        if c not in declared
    ]
    return bad


def unbound_pxd_classes(api: Api, mapping: dict[str, str]) -> list[str]:
    return sorted(set(api["classes"]) - set(mapping))


# The C++ containers the Python surface has a spelling for. A
# std::vector is a list, and a list is a repeated protobuf field. Only
# this one so far: std::set has the same Python spelling but loses the
# distinction, and std::map needs a key type the surface can carry.
_CONTAINERS = {"vector": "list"}


def map_c_type(raw: str, mapping: dict[str, str]) -> str:
    """Map a raw pxd type ('string', 'const CStorePath&', 'CValue*')
    to the Python annotation used by protocol dicts.

    A container maps element-first, and a pointer element maps like a
    value one: `vector[CStorePath *]` is `list[StorePath]`, because
    whether the binding holds each element by pointer is a question
    about C++ and not about the surface."""
    t = raw.strip()
    if t.startswith("const "):
        t = t[6:]
    if t.endswith(("&", "*")):
        t = t[:-1].rstrip()
    if t in _PRIMITIVES:
        return _PRIMITIVES[t]
    if t in mapping:
        return mapping[t]
    container, _, rest = t.partition("[")
    if container in _CONTAINERS and rest.endswith("]"):
        return (f"{_CONTAINERS[container]}"
                f"[{map_c_type(rest[:-1], mapping)}]")
    raise ValueError(
        f"unmapped pxd type {raw!r}: neither a primitive nor a class any "
        f"binding claims with _binds")


# The package a binding class comes from. A class from here is written
# bare, because every emitted module imports it from the package root;
# a class from anywhere else has to say where it lives.
BINDINGS_PKG = "cythonix_bindings"


def _qualified(cls: Any) -> str:
    """One resolved class, as the annotation that would name it.

    `__name__` alone is wrong for anything the emitted modules do not
    import from cythonix_bindings. `pathlib.Path` resolves to a class
    whose `__name__` is `Path`, and an emitted `-> Path` is a
    NameError - or worse, an import of `Path` from the bindings.

    That went unnoticed because it depends on something unrelated:
    get_type_hints resolves a whole function at once, so a method with
    a `StorePath` parameter fails to resolve (StorePath is a cimport,
    not a Python global) and keeps its written strings, while a method
    whose annotations all resolve loses every module. The same
    declaration meant two different things depending on its
    NEIGHBOURS.

    The module HEAD, not `__module__`, because that is what the author
    wrote and what a reader can import: `pathlib.Path` is really
    `pathlib._local.Path` on 3.14, and the private path is no annotation
    to emit. Checked rather than assumed - if the head does not
    re-export the class, the full module path is the honest answer."""
    name: str = cls.__name__
    mod = getattr(cls, "__module__", "") or ""
    if mod == "builtins" or mod.split(".")[0] in ("", BINDINGS_PKG):
        return name
    head = mod.split(".")[0]
    with contextlib.suppress(Exception):
        if getattr(importlib.import_module(head), name, None) is cls:
            return f"{head}.{name}"
    return f"{mod}.{name}"


def _annotation_name(ann: Any) -> str:
    """Stringify one annotation. The empty-check lives HERE so callers
    can pass either the resolved hint or the raw annotation - passing
    sig.return_annotation as a 'sentinel' argument was the bug that
    turned every annotated return into Any.

    A subscripted generic renders in full. `__name__` on one answers
    with the head - `dict[str, int]` says `dict` - which loses exactly
    the part that says what the entries hold. Whether that mattered
    depended on which path resolved the annotation: a free function's
    stayed the written string and kept its parameters, a method's
    resolved to a real generic and lost them, so the same declaration
    meant two different things depending on where it was written.

    A resolved CLASS renders through _qualified, so a type from
    outside the bindings keeps the module that says where it lives."""
    if ann is inspect.Signature.empty:
        return "Any"
    if ann is None or getattr(ann, "__name__", None) == "NoneType":
        return "None"
    origin = get_origin(ann)
    if origin is not None:
        inner = ", ".join(_annotation_name(a) for a in get_args(ann))
        return f"{_annotation_name(origin)}[{inner}]"
    if isinstance(ann, type):
        return _qualified(ann)
    return getattr(ann, "__name__", str(ann))


def default_source(value: Any, type_str: str, where: str) -> str | None:
    """One parameter default, as the source that reproduces it.

    A default is a fact about the SIGNATURE, not about the wire. Every
    generated surface writes it, so a caller that omits the argument
    gets the same value in-process and over RPC, and the argument that
    reaches the wire is always present. That is why this answers with
    source rather than with a value: the emitter writes it, the runtime
    never sees it.

    None means the parameter has no default.

    An enum member is written as the member, not as its value. A
    StrEnum member IS a string, so `repr` would give `'nar'` - which
    still calls correctly and which a typechecker rejects, because a
    str is not a ContentAddressMethod.

    Everything else must be a literal that reads back as itself. That
    check is not ceremony: `repr(float("inf"))` is `inf`, which is a
    NameError in the module it would be written into.

    A default of None is refused for anything but a CONTAINER. A proxy
    has no None to send and a scalar field has no presence, so a None
    default would typecheck here and fail at the first call that took
    it. A `list[T]` or a `dict[str, V]` is the case where it works: a
    repeated protobuf field has no presence problem, because an absent
    one and an empty one are the same field. So None crosses as
    nothing and the far side reads back the empty container it means.

    `[]` is the alternative and it is worse. It is a mutable default,
    and every generated surface would carry one - four shared lists
    where the binding has one.

    A CONSTRUCTOR parameter may still default to None whatever its
    type, and does not come through here: it comes from
    constructor_signature, where the overload set says a parameter may
    be omitted and C++ says nothing about what it would have been."""
    if value is inspect.Parameter.empty:
        return None
    if value is None:
        if head(type_str) in CONTAINERS:
            return "None"
        raise ValueError(
            f"{where}: a default of None needs an optional type the surface "
            f"cannot yet spell. {type_str} is not a container, so absence "
            f"has nothing to travel as. Declare the parameter required.")
    if isinstance(value, Enum):
        return f"{type(value).__name__}.{value.name}"
    if isinstance(value, (list, dict, set, bytearray)):
        # `repr([])` reads back as itself, so nothing below would stop
        # this. It is refused because of what it MEANS: one shared
        # mutable default per generated surface, four of them for a
        # binding that has one.
        raise ValueError(
            f"{where}: {value!r} is a mutable default, and every generated "
            f"surface would carry its own. Default the parameter to None - a "
            f"container reads an absent argument back as empty.")
    src = repr(value)
    try:
        if ast.literal_eval(src) != value:
            raise ValueError("does not read back as itself")
    except (ValueError, SyntaxError) as exc:
        raise ValueError(
            f"{where}: default {value!r} is not a literal the generated "
            f"surfaces can write ({exc})") from None
    return src


def _param(p: inspect.Parameter, type_str: str, where: str) -> Proto:
    """One parameter, as every layer above reads it."""
    return {
        "name": p.name,
        "type": type_str,
        "default": default_source(p.default, type_str, f"{where}:{p.name}"),
    }


def extract_method(func: Any) -> Proto:
    sig = inspect.signature(func)
    params = list(sig.parameters.values())[1:]  # drop self
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = getattr(func, "__annotations__", {})
    return {
        "name": func.__name__,
        "params": [
            _param(p,
                   _normalize(_annotation_name(hints.get(p.name, p.annotation))),
                   func.__name__)
            for p in params
        ],
        "return_type": _normalize(_annotation_name(hints.get("return", sig.return_annotation))),
        "doc": inspect.getdoc(func) or "",
    }


def extract_wrapper(cls: type, api: Api | None = None,
                    mapping: dict[str, str] | None = None,
                    constructible: bool = False) -> Proto:
    """
    Reflect the live Python surface across the cythonix_bindings MRO chain:
    every public method and property the bindings actually expose, with
    leaf definitions winning over inherited ones. This - not the pxd -
    is the contract users program against; the pxd only feeds type
    policies elsewhere.

    Cython's `str arg` signature typing yields NO runtime annotation,
    so parameter types fall back to "Any"; when api+mapping are given,
    those gaps are filled from the pxd declarations.
    """
    entries: dict[str, object] = {}
    for klass in reversed(cls.__mro__):
        mod = getattr(klass, "__module__", "")
        if mod.split(".")[0] != "cythonix_bindings":
            continue
        for name, val in klass.__dict__.items():
            if name.startswith("_"):
                continue
            entries[name] = val

    methods = []
    for name, val in entries.items():
        if callable(val):
            methods.append(extract_method(val))
        elif hasattr(val, "__get__"):
            # Readable attribute: a Python property, or a Cython getset
            # descriptor (how cdef classes compile @property). Both
            # resolve to a plain value on access.
            methods.append(_reader_method(name, val))
        # anything else (plain class attrs) is not part of the surface

    ctor: list[Proto] = []
    if api is not None and mapping is not None:
        # Returned types are produced, never constructed - their
        # __init__ raises - so their C++ overloads describe nothing a
        # caller can reach. Deriving a signature for them would ship a
        # plausible lie: StorePath's (string) means a base name while
        # its (string, string) means hash-plus-name, and the two merge
        # into a single wrong pair of optional strings.
        if constructible:
            ctor = constructor_signature(cls, api, mapping)
        table: Proto = _pxd_signature_table(cls, api, mapping)
        for m in methods:
            known = table.get(m["name"])
            if not known:
                continue
            for i, p in enumerate(m["params"]):
                if p["type"] == "Any" and i < len(known["params"]):
                    p["type"] = known["params"][i]
            if m["return_type"] == "Any" and known["ret"] is not None:
                m["return_type"] = known["ret"]

    return {
        "name": cls.__qualname__,
        "module": cls.__module__,
        # Read from __dict__, not inspect.getdoc: a class with no
        # docstring of its own would otherwise inherit its base's and
        # the stub would document LocalStore with Store's text.
        "doc": cls.__dict__.get("__doc__") or "",
        "binds": cls.__dict__.get("_binds", ""),
        "bases": [f"{b.__module__}.{b.__qualname__}" for b in cls.__bases__ if b is not object],
        "threading": getattr(cls, "_threading", "affine"),
        # Generated as a base class: carries the surface its subclasses
        # share, and is never constructed.
        "abstract": bool(cls.__dict__.get("_abstract", False)),
        # Produced by something else, never constructed: __init__
        # raises, so no surface may offer a constructor. Declared,
        # because it is a fact only the binding knows - a class the
        # pxd names as a method return type is produced today, which
        # is a proxy for this and true of the others by coincidence
        # (CMockStorePath declares constructors in the pxd and its
        # Python __init__ still raises).
        "produced": bool(cls.__dict__.get("_produced", False)),
        # Wire policy for the future RPC layer: "proxy" objects keep
        # identity and travel as handles; "value" objects are immutable
        # and travel serialized (locally emulated as copies). Default is
        # the safe one: stateful until proven immutable.
        "wire": getattr(cls, "_wire", "proxy"),
        # Serialization contract for wire-values: [[field, type], ...].
        # The proto message shape and both codecs derive from this, so
        # adding a wire-value type means editing the pyx and nothing
        # else. Empty for proxies, which travel as handles.
        "wire_fields": [list(f) for f in getattr(cls, "_wire_fields", ())],
        # How to walk this type as a TREE, when it is one. A value that
        # holds values cannot be described by _wire_fields: the shape is
        # recursive and its arms are the wire kinds themselves. The RPC
        # layer reads this instead of naming the class or its accessors
        # (tasks/030). Absent for everything that is not a tree.
        **({"tree": dict(cls.__dict__["_tree"])} if "_tree" in cls.__dict__ else {}),
        # Does this class need an async wrapper at all? Only two things
        # a wrapper buys: a hop onto a home thread, and releasing the
        # GIL around a call that waits. A pool class whose methods
        # cannot block gets neither, so it crosses every layer as
        # itself - no Async form, no RPC form, no await (tasks/025).
        # Inheritable on purpose, unlike _binds: a subclass of a
        # non-blocking class is non-blocking until it says otherwise.
        "blocking": bool(getattr(cls, "_blocking", True)),
        "wrapped": getattr(cls, "_threading", "affine") == "affine"
                   or bool(getattr(cls, "_blocking", True)),
        # Private round-trip helpers present on the class. Not part of
        # the surface; the contract check reads them.
        "_helpers": sorted(h for h in ("_parts", "_from_parts") if hasattr(cls, h)),
        # Typed construction, straight from the pxd - the only place a
        # Cython constructor's signature is visible at all.
        "ctor": ctor,
        "methods": methods,
    }


def constructor_signature(cls: type, api: Api,
                          mapping: dict[str, str]) -> list[Proto]:
    """The typed parameter list for constructing `cls`.

    The pxd is the ONLY source. Cython exposes no signature for
    __cinit__ - no __text_signature__, nothing in __dict__ - so
    inspect.signature reports (self, /, *args, **kwargs) for every
    binding class alike. Reflection cannot contribute one bit here,
    which is why the server's old "does the constructor need
    arguments?" check was a constant True wearing a disguise.

    C++ gives an overload SET where Python takes one signature. They
    reconcile when the overloads are type-prefix compatible - each a
    positional prefix of the next - which is what an optional-argument
    constructor looks like once C++ has spelled it out:

        CDerivedPath(CStorePath)
        CDerivedPath(CStorePath, string)   ->  (path, output=None)

    Parameters past the shortest overload are optional. Names come from
    the longest overload; C++ overloads often rename (path/drv_path)
    and the longest one is the most descriptive.

    The overload set is the ONLY source of optionality here, because a
    pxd cannot carry a real default at all - Cython refuses one
    outright: "default values cannot be specified in pxd files, use ?
    or *". What it does accept is `int n = *`, which says a parameter
    may be omitted without saying what it would have been. That is the
    same fact the overload set gives, and it is the mechanism to reach
    for if a _ctor_from FACTORY ever needs an optional parameter -
    there is no overload set to read for one of those.

    Raises when the overloads do NOT reconcile, naming the class. That
    is a real ambiguity - CStorePath's (string) means a base name while
    its (string, string) means hash-plus-name - and the author has to
    say which one Python offers.

    A class with no declared constructor takes none: C++ gives it an
    implicit default and the binding's __cinit__ matches.
    """
    factory = cls.__dict__.get("_ctor_from")
    if factory is not None:
        # Some types are not constructed at all: nix::Store is abstract
        # and openStore(uri) picks the implementation. There is no
        # constructor for the pxd to describe, so the binding names the
        # FACTORY and its parameters are the ones a caller passes.
        for fn in api.get("free_functions", []):
            if fn["name"] == factory:
                return [
                    {"name": pname, "type": map_c_type(ptype, mapping),
                     "default": None}
                    for pname, ptype in fn["params"]
                ]
        raise ValueError(
            f"{cls.__name__}._ctor_from = {factory!r}: no free function of "
            f"that name in the pxd")

    info = api["classes"].get(cls.__dict__.get("_binds", ""))
    if info is None:
        return []
    overloads = sorted(info["ctors"], key=len)
    if not overloads:
        return []
    longest = overloads[-1]
    for shorter in overloads[:-1]:
        for i, (_, ptype) in enumerate(shorter):
            if map_c_type(ptype, mapping) != map_c_type(longest[i][1], mapping):
                raise ValueError(
                    f"{cls.__name__}: constructor overloads do not reconcile "
                    f"into one Python signature. Overload "
                    f"{[t for _, t in shorter]} is not a prefix of "
                    f"{[t for _, t in longest]}; they differ at position {i}. "
                    f"Declare which one the binding offers.")
    required = len(overloads[0])
    # Past the shortest overload the parameter may be omitted, and C++
    # says nothing about what it would have been - the short overload
    # simply does not take it. So the default is None, meaning "not
    # given", and it is written as source like any other so a
    # constructor parameter and a method parameter have ONE shape.
    return [
        {
            "name": pname,
            "type": map_c_type(ptype, mapping),
            "default": "None" if i >= required else None,
        }
        for i, (pname, ptype) in enumerate(longest)
    ]


def extract_free_function(fn: Any, api: Api,
                          mapping: dict[str, str]) -> Proto:
    """Protocol dict for a module-level binding function.

    Free functions introspect FAR better than cdef classes: real
    signatures, real annotations, real docstrings, and a settable
    __dict__. So the live function supplies almost everything, and the
    pxd fills only what Cython drops - untyped parameters, exactly as
    it does for methods.

    Threading is checked, not merely read. A free function has no
    instance and therefore no home thread, so "pool" is the only policy
    that means anything; anything else is a mistake worth naming.

    A function with NO policy is still described here. It gets no async
    form and no rpc - that is what declaring nothing means - but it is
    part of the module's public surface, so anything that describes
    that surface (the stubs, 027) has to know its signature. Leaving it
    out would make a stub package that silently hides a real name."""
    policy = getattr(fn, "_threading", None)
    if policy is not None and policy != "pool":
        raise ValueError(
            f"{fn.__name__}: a free function may only declare _threading = "
            f"'pool' (got {policy!r}). It has no instance, so there is "
            f"no thread for it to be affine to.")

    sig = inspect.signature(fn)
    hints = getattr(fn, "__annotations__", {})
    params = [
        _param(p,
               _normalize(_annotation_name(hints.get(p.name, p.annotation))),
               fn.__name__)
        for p in sig.parameters.values()
    ]
    declared = {f["name"]: f for f in api.get("free_functions", [])}
    c_name = getattr(fn, "_binds", fn.__name__)
    known = declared.get(c_name)
    if known is None and hasattr(fn, "_binds"):
        raise ValueError(
            f"{fn.__name__}._binds = {c_name!r}: no free function of that "
            f"name in the pxd (declared: {sorted(declared)})")
    if known is not None:
        for i, p in enumerate(params):
            if p["type"] == "Any" and i < len(known["params"]):
                p["type"] = map_c_type(known["params"][i][1], mapping)

    return {
        "name": fn.__name__,
        "module": fn.__module__,
        "threading": policy,
        # No policy means no wrapper: the function is surface, not
        # something the codegen hops a thread for.
        "wrapped": policy is not None,
        "params": params,
        "return_type": _normalize(_annotation_name(hints.get("return", sig.return_annotation))),
        "doc": inspect.getdoc(fn) or "",
    }


def check_wire_contract(protos: list[Proto]) -> list[str]:
    """The wire policy and the serialization contract must agree.

    A "value" type promises the RPC layer it can be rebuilt from its
    parts; a "proxy" promises it cannot and must stay behind a handle.
    A value with no _wire_fields, or missing round-trip helpers, used to
    surface as a KeyError deep inside the server on the first call that
    touched it. Fail the build instead, naming the type.

    Returns a list of complaints; empty means the contract holds."""
    known = {p["name"] for p in protos}
    kinds = {p["name"]: p["wire"] for p in protos}
    bad = []
    for proto in protos:
        name, fields = proto["name"], proto["wire_fields"]
        if proto["wire"] == "proxy":
            if fields:
                bad.append(f"{name}: proxy types travel as handles, drop _wire_fields")
            continue
        if proto["wire"] != "value":
            bad.append(f"{name}: unknown _wire {proto['wire']!r} (value|proxy)")
            continue
        if not fields:
            bad.append(f"{name}: wire-value needs _wire_fields describing its message")
        if proto["threading"] != "pool":
            # A value that may not leave its thread cannot be serialised
            # off it; the two policies contradict each other.
            bad.append(
                f"{name}: wire-value must be threading 'pool', not "
                f"{proto['threading']!r}")
        for fname, ftype in fields:
            optional = ftype.endswith("?")
            ftype = ftype.removesuffix("?")
            # A container field is a repeated protobuf field, so what
            # goes under test is the type it HOLDS. The declaration is
            # the same one an annotation uses - `list[StorePath]` - and
            # it is read by the same code, so the two cannot drift.
            try:
                element = list_value(ftype) or map_value(ftype)
            except TypeError as e:
                bad.append(f"{name}._wire_fields {fname!r}: {e}")
                continue
            if element is not None:
                if optional:
                    # A repeated field has no presence, so an absent
                    # one and an empty one are the same field. "?"
                    # would promise a distinction that cannot exist.
                    bad.append(
                        f"{name}._wire_fields {fname!r}: a container cannot "
                        f"be optional. A repeated field has no presence, so "
                        f"an absent one IS an empty one - drop the '?'.")
                ftype = element
            if ftype not in _PRIMITIVES.values() and ftype not in known:
                bad.append(f"{name}._wire_fields {fname!r}: unknown field type {ftype!r}")
            elif kinds.get(ftype) == "proxy":
                # The schema would carry it: _msg_arg_type turns a proxy
                # into a Handle field wherever it appears. The codec
                # cannot, and not by omission. A wire-value is rebuilt
                # on the far side by _from_parts, which needs a real
                # local object for every part - and a proxy is exactly
                # the thing that has no object on the far side. Nesting
                # one here produces a type that only its own process can
                # reconstruct. Say so at build time rather than at the
                # first call that touches the field (tasks/031).
                bad.append(
                    f"{name}._wire_fields {fname!r}: {ftype} is a proxy, so "
                    f"_from_parts has nothing to rebuild it from on the far "
                    f"side. A wire-value copies all the way down. Carry the "
                    f"proxy as a method parameter or return instead.")
        for helper in ("_parts", "_from_parts"):
            if helper not in proto["_helpers"]:
                bad.append(f"{name}: wire-value needs a {helper} round-trip helper")
    return bad


def extract_enum(cls: type) -> Proto:
    """One string vocabulary, as the manifest carries it.

    The values, so a reader can see what the surface accepts, and the
    module, so a stub that NAMES the type can import it. Nothing about
    how it crosses: a member is a str, and that is the whole answer."""
    return {
        "name": cls.__name__,
        "module": cls.__module__,
        "values": [str(m.value) for m in cls],  # type: ignore[var-annotated]
        "doc": inspect.getdoc(cls) or "",
    }


def extract_errors(bindings: ModuleType) -> Proto:
    """The exception hierarchy a binding can raise, as the manifest
    carries it.

    Read from the module the package DECLARES in `_errors_module`, not
    from a name this file knows. A package with no such declaration has
    no error surface, which is a legitimate answer: the generator does
    not require a library to have one.

    What crosses is the class NAME, and the point of recording the set
    here is that a name is only safe to construct against a declared
    one. Without it the alternative is a status message that says which
    module to import, which lets the far side name any importable
    class."""
    module_name = getattr(bindings, "_errors_module", None)
    if module_name is None:
        return {"module": None, "classes": {}}
    module = importlib.import_module(module_name)
    classes: Proto = {}
    for name, kls in sorted(vars(module).items()):
        if not (isinstance(kls, type) and issubclass(kls, BaseException)):
            continue
        if kls.__module__ != module_name:
            continue  # imported, not declared here
        classes[name] = {
            # Bases INSIDE this module, so a reader can see the
            # hierarchy without importing anything. Python gives the
            # real one on import; this is for describing the surface.
            "bases": [b.__name__ for b in kls.__bases__
                      if b.__module__ == module_name],
            "wire_fields": [list(f) for f in getattr(kls, "_wire_fields", ())],
        }
    return {"module": module_name, "classes": classes}


def check_error_contract(errors: Proto) -> list[str]:
    """Every declared error must survive its own round trip.

    An error crosses as its declared parts and comes back as
    `cls(*parts)`, so the parts have to reach the attributes they are
    named after. Nothing static proves that - so this builds one of
    each and reads it back. A class whose __init__ reorders, renames or
    drops a part fails the BUILD rather than the first remote failure,
    which is the one moment nobody is watching for a bug.

    Returns a list of complaints; empty means the contract holds."""
    module_name = errors["module"]
    if module_name is None:
        return []
    module = importlib.import_module(module_name)
    bad = []
    for name, proto in errors["classes"].items():
        fields = proto["wire_fields"]
        if not fields:
            bad.append(
                f"{name}: no _wire_fields, so nothing says how to rebuild it "
                f"on the far side")
            continue
        kls = getattr(module, name)
        # Distinct values, so a swap is visible. A reordering that kept
        # the same string in both slots would otherwise pass.
        probe = [f"<{fname}>" for fname, _ in fields]
        try:
            built = kls(*probe)
        except Exception as e:
            bad.append(f"{name}: cannot be rebuilt as cls(*parts): {e}")
            continue
        for (fname, _), sent in zip(fields, probe, strict=True):
            got = getattr(built, fname, None)
            if got != sent:
                bad.append(
                    f"{name}._wire_fields names {fname!r}, but building it "
                    f"from its parts leaves {fname} = {got!r}, not {sent!r}")
    return bad


def check_wrap_contract(protos: list[Proto]) -> list[str]:
    """An unwrapped class must be self-contained.

    Not wrapping a class means callers touch the sync binding object
    directly. Two things then have to hold. It may not be affine -
    there would be no thread to hop to - which the wrapped rule already
    guarantees. And it may not hand back an object that IS wrapped: the
    caller would receive a bare sync instance of a type that needs a
    runner, with no runner attached and no await to get one.

    Returns a list of complaints; empty means the contract holds."""
    wrapped = {p["name"] for p in protos if p["wrapped"]}
    bad = []
    for proto in protos:
        if proto["wrapped"]:
            continue
        if proto["threading"] != "pool":
            bad.append(
                f"{proto['name']}: an unwrapped class must be threading "
                f"'pool', not {proto['threading']!r}")
        for m in proto["methods"]:
            if m["return_type"] in wrapped:
                bad.append(
                    f"{proto['name']}.{m['name']} returns {m['return_type']}, "
                    f"which needs a wrapper. An unwrapped class cannot "
                    f"attach one; declare _blocking on one side or the "
                    f"other so the two agree.")
    return bad


def check_collection_contract(protos: list[Proto]) -> list[str]:
    """No method may return a COLLECTION of wrapped types.

    A wrapped type only works when something attaches a runner to it,
    and every layer does that for one object: the async wrapper writes
    `AsyncX(result, self._runner)`, the server puts one handle, the
    client builds one proxy. None of them walks a container, so
    `dict[str, Value]` type-checks, builds, emits a schema - and hands
    back bare sync objects in process while failing on the first call
    over the wire.

    That is the same accepted-then-explodes shape a proxy in
    _wire_fields had. It is not an oversight either: a collection of
    remote objects is a value TREE, which is what Realize answers, and
    that is protocol rather than something a return annotation can ask
    for (tasks/030).

    Returns a list of complaints; empty means the contract holds."""
    wrapped = {p["name"] for p in protos if p["wrapped"]}
    bad = []
    for proto in protos:
        for m in proto["methods"]:
            rt = m["return_type"]
            if rt in wrapped:
                continue  # a single wrapped object is the supported case
            for named in sorted(names_in(rt) & wrapped):
                bad.append(
                    f"{proto['name']}.{m['name']} returns {rt}, a collection "
                    f"holding {named}. Nothing attaches a runner to the "
                    f"elements of a container. Return the container's owner "
                    f"and let the caller walk it, or realize it as a value "
                    f"tree (tasks/030).")
    return bad


def _pxd_signature_table(cls: type, api: Api,
                         mapping: dict[str, str]) -> Proto:
    """method name -> {'params': [python type names], 'ret': python type
    name or None}, gathered from every cythonix_bindings base in the MRO,
    using the pxd declarations.

    Walks the MRO, which is leaf-first, so an override wins over the
    declaration it shadows. Iterating the pxd's own class order instead
    let whichever class the file happened to declare first win - the
    base, as it happens, which is the opposite of what extract_wrapper
    promises."""
    out: dict[str, Proto] = {}
    for k in cls.__mro__:
        if getattr(k, "__module__", "").split(".")[0] != "cythonix_bindings":
            continue
        # _binds, not "C" + the class name. The convention was the
        # only link between the pxd and the bindings until tasks/020
        # replaced it with a declaration, and this lookup was missed:
        # a class whose _binds did not happen to match the convention
        # got NO signature backfill, silently, and its parameters
        # stayed Any. Read from __dict__ so a subclass does not
        # inherit its base's declaration.
        info = api["classes"].get(k.__dict__.get("_binds", ""))
        if info is None:
            continue
        for m in info["methods"]:
            # Unmapped types are fatal on BOTH sides now. A swallowed
            # return type silently left the surface un-backfilled, so a
            # missing _binds surfaced as a mysterious "Any" much later -
            # or not at all, when a live annotation happened to cover.
            out.setdefault(
                m["name"],
                {
                    "params": [
                        map_c_type(ptype, mapping) for _, ptype in m["params"]
                    ],
                    "ret": map_c_type(m["ret"], mapping),
                },
            )
    return out


def _reader_method(name: str, val: Any) -> Proto:
    """Protocol dict for a readable attribute: a zero-arg read. Setters
    are not surfaced yet."""
    ret = "Any"
    doc = ""
    fget = getattr(val, "fget", None)
    if fget is not None:
        # An unreadable signature just means the type stays Any.
        with contextlib.suppress(TypeError, ValueError):
            ret = _normalize(
                _annotation_name(inspect.signature(fget).return_annotation))
        doc = inspect.getdoc(fget) or inspect.getdoc(val) or ""
    return {"name": name, "params": [], "return_type": ret, "doc": doc}


def returned_types_from_api(api: Api, bindings_module: ModuleType,
                            mapping: dict[str, str]) -> list[type]:
    """Binding classes that appear as a method return type in the pxd
    AND carry a _threading marker — values handed back across the
    wrapper surface, as opposed to entry points users construct."""
    bound = set(mapping.values())
    names: set[str] = set()
    for info in api["classes"].values():
        for m in info["methods"]:
            # Every name in the return type, not the type itself: a
            # method returning list[MockStorePath] hands back
            # MockStorePaths as surely as one returning a single
            # MockStorePath does.
            for py in names_in(map_c_type(m["ret"], mapping)):
                if py not in bound:
                    continue  # a scalar, or the container itself
                kls = getattr(bindings_module, py, None)
                if isinstance(kls, type) and hasattr(kls, "_threading"):
                    names.add(py)
    return [getattr(bindings_module, n) for n in sorted(names)]
