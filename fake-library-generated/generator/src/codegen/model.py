"""
Introspect binding classes into plain protocol dicts.

No I/O, no ast — pure reflection plus the parsed pxd surface handed in
by the caller. The dict shape is the contract between the sources
(the bindings pxd + the installed bindings) and the emitter (emitter.py).
"""

import inspect
from typing import Any, get_type_hints

_PRIMITIVES = {
    "string": "str",
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


def binding_map(bindings_module) -> dict[str, str]:
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


def check_binding_map(api: dict, mapping: dict[str, str]) -> list[str]:
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


def unbound_pxd_classes(api: dict, mapping: dict[str, str]) -> list[str]:
    return sorted(set(api["classes"]) - set(mapping))


def map_c_type(raw: str, mapping: dict[str, str]) -> str:
    """Map a raw pxd type ('string', 'const CStorePath&', 'CValue*')
    to the Python annotation used by protocol dicts."""
    t = raw.strip()
    if t.startswith("const "):
        t = t[6:]
    if t.endswith("&") or t.endswith("*"):
        t = t[:-1].rstrip()
    if t in _PRIMITIVES:
        return _PRIMITIVES[t]
    if t in mapping:
        return mapping[t]
    raise ValueError(
        f"unmapped pxd type {raw!r}: neither a primitive nor a class any "
        f"binding claims with _binds")


def _annotation_name(ann) -> str:
    """Stringify one annotation. The empty-check lives HERE so callers
    can pass either the resolved hint or the raw annotation - passing
    sig.return_annotation as a 'sentinel' argument was the bug that
    turned every annotated return into Any."""
    if ann is inspect.Signature.empty:
        return "Any"
    if ann is None or getattr(ann, "__name__", None) == "NoneType":
        return "None"
    return getattr(ann, "__name__", str(ann))


def extract_method(func) -> dict:
    sig = inspect.signature(func)
    params = list(sig.parameters.values())[1:]  # drop self
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = getattr(func, "__annotations__", {})
    return {
        "name": func.__name__,
        "params": [
            {"name": p.name, "type": _normalize(_annotation_name(hints.get(p.name, p.annotation)))}
            for p in params
        ],
        "return_type": _normalize(_annotation_name(hints.get("return", sig.return_annotation))),
        "doc": inspect.getdoc(func) or "",
    }


def extract_wrapper(cls, api=None, mapping=None, constructible=False) -> dict:
    """
    Reflect the live Python surface across the fake_library MRO chain:
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
        if mod.split(".")[0] != "fake_library":
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

    ctor: list[dict] = []
    if api is not None and mapping is not None:
        # Returned types are produced, never constructed - their
        # __init__ raises - so their C++ overloads describe nothing a
        # caller can reach. Deriving a signature for them would ship a
        # plausible lie: StorePath's (string) means a base name while
        # its (string, string) means hash-plus-name, and the two merge
        # into a single wrong pair of optional strings.
        if constructible:
            ctor = constructor_signature(cls, api, mapping)
        table = _pxd_signature_table(cls, api, mapping)
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
        "bases": [f"{b.__module__}.{b.__qualname__}" for b in cls.__bases__ if b is not object],
        "threading": getattr(cls, "_threading", "affine"),
        # Generated as a base class: carries the surface its subclasses
        # share, and is never constructed.
        "abstract": bool(cls.__dict__.get("_abstract", False)),
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


def constructor_signature(cls, api: dict, mapping: dict[str, str]) -> list[dict]:
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

    Raises when the overloads do NOT reconcile, naming the class. That
    is a real ambiguity - CStorePath's (string) means a base name while
    its (string, string) means hash-plus-name - and the author has to
    say which one Python offers.

    A class with no declared constructor takes none: C++ gives it an
    implicit default and the binding's __cinit__ matches.
    """
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
    return [
        {
            "name": pname,
            "type": map_c_type(ptype, mapping),
            "optional": i >= required,
        }
        for i, (pname, ptype) in enumerate(longest)
    ]


def extract_free_function(fn, api: dict, mapping: dict[str, str]) -> dict:
    """Protocol dict for a module-level binding function.

    Free functions introspect FAR better than cdef classes: real
    signatures, real annotations, real docstrings, and a settable
    __dict__. So the live function supplies almost everything, and the
    pxd fills only what Cython drops - untyped parameters, exactly as
    it does for methods.

    Threading is checked, not merely read. A free function has no
    instance and therefore no home thread, so "pool" is the only policy
    that means anything; anything else is a mistake worth naming."""
    policy = getattr(fn, "_threading", None)
    if policy != "pool":
        raise ValueError(
            f"{fn.__name__}: free functions must declare _threading = "
            f"'pool' (got {policy!r}). They have no instance, so there is "
            f"no thread for them to be affine to.")

    sig = inspect.signature(fn)
    hints = getattr(fn, "__annotations__", {})
    params = [
        {"name": p.name,
         "type": _normalize(_annotation_name(hints.get(p.name, p.annotation)))}
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
        "params": params,
        "return_type": _normalize(_annotation_name(hints.get("return", sig.return_annotation))),
        "doc": inspect.getdoc(fn) or "",
    }


def check_wire_contract(protos: list[dict]) -> list[str]:
    """The wire policy and the serialization contract must agree.

    A "value" type promises the RPC layer it can be rebuilt from its
    parts; a "proxy" promises it cannot and must stay behind a handle.
    A value with no _wire_fields, or missing round-trip helpers, used to
    surface as a KeyError deep inside the server on the first call that
    touched it. Fail the build instead, naming the type.

    Returns a list of complaints; empty means the contract holds."""
    known = {p["name"] for p in protos}
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
            ftype = ftype.removesuffix("?")
            if ftype not in _PRIMITIVES.values() and ftype not in known:
                bad.append(f"{name}._wire_fields {fname!r}: unknown field type {ftype!r}")
        for helper in ("_parts", "_from_parts"):
            if helper not in proto["_helpers"]:
                bad.append(f"{name}: wire-value needs a {helper} round-trip helper")
    return bad


def check_wrap_contract(protos: list[dict]) -> list[str]:
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


def _pxd_signature_table(cls, api: dict, mapping: dict[str, str]) -> dict:
    """method name -> {'params': [python type names], 'ret': python type
    name or None}, gathered from every fake_library base in the MRO,
    using the pxd declarations.

    Walks the MRO, which is leaf-first, so an override wins over the
    declaration it shadows. Iterating the pxd's own class order instead
    let whichever class the file happened to declare first win - the
    base, as it happens, which is the opposite of what extract_wrapper
    promises."""
    out: dict[str, dict] = {}
    for k in cls.__mro__:
        if getattr(k, "__module__", "").split(".")[0] != "fake_library":
            continue
        info = api["classes"].get("C" + k.__name__)
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


def _reader_method(name: str, val) -> dict:
    """Protocol dict for a readable attribute: a zero-arg read. Setters
    are not surfaced yet."""
    ret = "Any"
    doc = ""
    fget = getattr(val, "fget", None)
    if fget is not None:
        try:
            ret = _normalize(_annotation_name(inspect.signature(fget).return_annotation))
        except (TypeError, ValueError):
            pass
        doc = inspect.getdoc(fget) or inspect.getdoc(val) or ""
    return {"name": name, "params": [], "return_type": ret, "doc": doc}


def returned_types_from_api(api: dict, bindings_module,
                            mapping: dict[str, str]) -> list[type]:
    """Binding classes that appear as a method return type in the pxd
    AND carry a _threading marker — values handed back across the
    wrapper surface, as opposed to entry points users construct."""
    bound = set(mapping.values())
    names: set[str] = set()
    for info in api["classes"].values():
        for m in info["methods"]:
            py = map_c_type(m["ret"], mapping)
            if py not in bound:
                continue  # a scalar
            kls = getattr(bindings_module, py, None)
            if isinstance(kls, type) and hasattr(kls, "_threading"):
                names.add(py)
    return [getattr(bindings_module, n) for n in sorted(names)]
