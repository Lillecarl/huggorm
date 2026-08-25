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


def map_c_type(raw: str, bindings_module) -> str:
    """Map a raw pxd type ('string', 'const CPoop&', 'CPoop*', 'CPoop')
    to the Python annotation used by protocol dicts."""
    t = raw.strip()
    if t.startswith("const "):
        t = t[6:]
    if t.endswith("&") or t.endswith("*"):
        t = t[:-1].rstrip()
    if t in _PRIMITIVES:
        return _PRIMITIVES[t]
    if t.startswith("C"):
        candidate = t[1:]
        if hasattr(bindings_module, candidate):
            # Bound wrapper types are validated by the caller (they must
            # carry a _threading marker to be usable as return values;
            # plain parameter use is fine regardless).
            return candidate
    raise ValueError(f"unmapped pxd type {raw!r}")


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


def extract_wrapper(cls, api=None, bindings=None, hide=()) -> dict:
    """
    Reflect the live Python surface across the fake_library MRO chain:
    every public method and property the bindings actually expose, with
    leaf definitions winning over inherited ones. This - not the pxd -
    is the contract users program against; the pxd only feeds type
    policies elsewhere. `hide` drops names from the emitted surface.

    Cython's `str arg` signature typing yields NO runtime annotation,
    so parameter types fall back to "Any"; when api+bindings are given,
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
        if name in hide:
            continue
        if callable(val):
            methods.append(extract_method(val))
        elif hasattr(val, "__get__"):
            # Readable attribute: a Python property, or a Cython getset
            # descriptor (how cdef classes compile @property). Both
            # resolve to a plain value on access.
            methods.append(_reader_method(name, val))
        # anything else (plain class attrs) is not part of the surface

    if api is not None and bindings is not None:
        table = _pxd_signature_table(cls, api, bindings)
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
        # Private round-trip helpers present on the class. Not part of
        # the surface; the contract check reads them.
        "_helpers": sorted(h for h in ("_parts", "_from_parts") if hasattr(cls, h)),
        "methods": methods,
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


def _pxd_signature_table(cls, api: dict, bindings_module) -> dict:
    """method name -> {'params': [python type names], 'ret': python type
    name or None}, gathered from every fake_library base in the MRO,
    using the pxd declarations."""
    wanted = {"C" + k.__name__ for k in cls.__mro__
              if getattr(k, "__module__", "").split(".")[0] == "fake_library"}
    out: dict[str, dict] = {}
    for key, info in api["classes"].items():
        if key not in wanted:
            continue
        for m in info["methods"]:
            try:
                ret = map_c_type(m["ret"], bindings_module)
            except ValueError:
                ret = None
            out.setdefault(
                m["name"],
                {
                    "params": [
                        map_c_type(ptype, bindings_module) for _, ptype in m["params"]
                    ],
                    "ret": ret,
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


def returned_types_from_api(api: dict, bindings_module) -> list[type]:
    """Binding classes that appear as a method return type in the pxd
    AND carry a _threading marker — values handed back across the
    wrapper surface, as opposed to entry points users construct."""
    names: set[str] = set()
    for info in api["classes"].values():
        for m in info["methods"]:
            try:
                py = map_c_type(m["ret"], bindings_module)
            except ValueError:
                continue
            kls = getattr(bindings_module, py, None)
            if isinstance(kls, type) and hasattr(kls, "_threading"):
                names.add(py)
    return [getattr(bindings_module, n) for n in sorted(names)]
