"""
The protocol dict, and the rules it has to obey.

The dict shape is the contract between the declarations (which build
it, in `cythonix_idl.manifest`) and the emitter (emitter.py). This
file holds what neither of them owns: how one annotation is spelled,
how one default is written back as source, and the `check_*` functions
the build refuses to pass.

Nothing here reflects a binding class any more. `extract_wrapper` did,
and it went when the last class it could measure stopped existing -
a nanobind method is a builtin with no signature, so reflection has
nothing to read. `extract_errors` still imports a module, because an
exception hierarchy is plain Python and is not declared.
"""

import ast
import contextlib
import importlib
import inspect
from enum import Enum
from types import ModuleType
from typing import Any, get_args, get_origin

from codegen.wiretypes import (
    CONTAINERS,
    head,
    list_value,
    map_value,
    names_in,
    optional_value,
)

# One class or function, reflected into the plain dict every layer
# above reads. Named rather than spelled dict[str, Any] everywhere:
# it is the contract between the sources and the emitter, and the one
# place to tighten if it becomes a TypedDict (tasks/029).
Proto = dict[str, Any]

# The package the bindings live in. A type declared there is spelled
# bare in a signature, because the module that names it is the one
# that defines it.
BINDINGS_PKG = "cythonix_bindings"

# The dunders a value type may define, and the three it must. Ordering
# and __str__ are per-class: a store path has a natural order and a
# natural string, a PathInfo has neither.
VALUE_DUNDERS = ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__",
                 "__hash__", "__repr__", "__str__")
REQUIRED_DUNDERS = ("__eq__", "__hash__", "__repr__")

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
    # <stdint.h> spellings. A declaration says `I64` or `U64`, and
    # `manifest.PYTHON` maps the C++ onto `int` before the name gets
    # here - so these are what a field type is checked against.
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

def _qualified(cls: Any) -> str:
    """One resolved class, as the annotation that would name it.

    `__name__` alone is wrong for anything the emitted modules do not
    import from cythonix_bindings. `pathlib.Path` resolves to a class
    whose `__name__` is `Path`, and an emitted `-> Path` is a
    NameError - or worse, an import of `Path` from the bindings.

    That went unnoticed because it depends on something unrelated:
    get_type_hints resolves a whole function at once, so a method with
    a `StorePath` parameter can fail to resolve (the name is not a
    Python global) and keeps its written strings, while a method
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


def check_wire_contract(protos: list[Proto],
                        enums: set[str] | None = None) -> list[str]:
    """The wire policy and the serialization contract must agree.

    A "value" type promises the RPC layer it can be rebuilt from its
    parts; a "proxy" promises it cannot and must stay behind a handle.
    A value with no _wire_fields, or missing round-trip helpers, used to
    surface as a KeyError deep inside the server on the first call that
    touched it. Fail the build instead, naming the type.

    Returns a list of complaints; empty means the contract holds."""
    # A string enum is a NAME the wire knows and not a class in the
    # manifest's groups, so it has to be handed in. Without it a
    # _wire_fields entry of enum type failed as "unknown field type" -
    # refused here while the rpc layer accepted the same declaration
    # anywhere a scalar goes (tasks/047).
    known = {p["name"] for p in protos} | (enums or set())
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
        for dunder in REQUIRED_DUNDERS:
            if dunder not in proto["dunders"]:
                # A value that does not compare is a value in name
                # only: two of them naming the same thing are unequal,
                # a set of them deduplicates nothing, and repr() shows
                # an address. All three answers are derivable from the
                # _wire_fields this class already declares - see
                # _value.py - so there is nothing to weigh up.
                bad.append(
                    f"{name}: wire-value needs {dunder}. A value compares, "
                    f"hashes and prints as what it is, and _value.py "
                    f"derives all three from _wire_fields.")
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


def check_optional_contract(protos: list[Proto]) -> list[str]:
    """An optional return may name a value, never a wrapped type.

    `T | None` works because a protobuf message field has presence, so
    the wire needs nothing new. What has no answer is the layer above
    it: every layer adopts a wrapped return into a runner - the async
    wrapper writes `AsyncX(result, self._runner)`, the server puts a
    handle, the client builds a proxy - and none of them adopts
    nothing.

    grpc_schema already refuses it for the WIRE, but that would leave
    the in-process wrapper handing back a bare sync object with no
    runner attached: accepted, built, and wrong at the first await.
    So it is refused here, for every surface at once.

    Returns a list of complaints; empty means the contract holds."""
    wrapped = {p["name"] for p in protos if p["wrapped"]}
    bad = []
    for proto in protos:
        for m in proto["methods"]:
            try:
                inner = optional_value(m["return_type"])
            except TypeError as e:
                bad.append(f"{proto['name']}.{m['name']}: {e}")
                continue
            if inner is not None and inner in wrapped:
                bad.append(
                    f"{proto['name']}.{m['name']} returns {m['return_type']}, "
                    f"and {inner} needs a runner attached. Every layer adopts "
                    f"one object and none of them adopts nothing. Raise "
                    f"instead, or return a wire value.")
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
