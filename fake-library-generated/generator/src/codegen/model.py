"""
Introspect IDL classes into plain protocol dicts.

No I/O, no ast — pure reflection plus the parsed pxd surface handed in
by the caller. The dict shape is the contract between the IDL
(spec.py + c_animal.pxd) and the emitter (emitter.py).
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
    "void": "None",
}


def map_c_type(raw: str, bindings_module) -> str:
    """Map a raw pxd type ('string', 'const CPoop&', 'CPoop') to the
    Python annotation used by protocol dicts."""
    t = raw.strip()
    if t.startswith("const "):
        t = t[6:]
    if t.endswith("&"):
        t = t[:-1]
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
            {"name": p.name, "type": _annotation_name(hints.get(p.name, p.annotation))}
            for p in params
        ],
        "return_type": _annotation_name(hints.get("return", sig.return_annotation)),
        "doc": inspect.getdoc(func) or "",
    }


def extract_service(cls, inherited_methods=None, hide=()) -> dict:
    """
    own methods via reflection; pxd-derived inherited methods are appended
    (they never shadow own definitions) minus the `hide` set.
    """
    own = {
        name: val
        for name, val in cls.__dict__.items()
        if not name.startswith("_") and callable(val)
    }
    marked = any(getattr(v, "_exposed", False) for v in own.values())
    if marked:
        own = {n: v for n, v in own.items() if getattr(v, "_exposed", False)}

    methods = [extract_method(f) for f in own.values()]
    seen = {m["name"] for m in methods}
    for m in inherited_methods or []:
        if m["name"] in seen or m["name"] in hide:
            continue
        methods.append(m)
        seen.add(m["name"])

    return {
        "service": cls.__qualname__,
        "module": cls.__module__,
        "bases": [f"{b.__module__}.{b.__qualname__}" for b in cls.__bases__ if b is not object],
        "threading": getattr(cls, "_threading", "affine"),
        "methods": methods,
    }


def binding_base_chain(cls, bindings_module_name: str = "fake_library"):
    """Yield the Cython wrapper classes this service inherits from."""
    for base in cls.__mro__[1:]:
        mod = getattr(base, "__module__", "")
        if mod.split(".")[0] == bindings_module_name:
            yield base


def inherited_from_pxd(service_cls, api: dict, bindings_module) -> list[dict]:
    """
    Build protocol method dicts for every method the service's binding
    bases declare in the pxd. Types come from the DECLARATIONS - full
    fidelity, no compiled-artifact introspection limits.
    """
    classes = api["classes"]
    chain: list[str] = []
    for base in binding_base_chain(service_cls):
        key = "C" + base.__name__
        if key in classes:
            chain.append(key)

    out: dict[str, dict] = {}
    visited: set[str] = set()
    while chain:
        key = chain.pop(0)
        if key in visited:
            continue
        visited.add(key)
        info = classes[key]
        chain.extend(b for b in info["bases"] if b not in visited)
        for m in info["methods"]:
            if m["name"] in out:
                continue
            live = getattr(bindings_module, _binding_class_name(key), None)
            doc = inspect.getdoc(getattr(live, m["name"], None)) or "" if live else ""
            out[m["name"]] = {
                "name": m["name"],
                "params": [
                    {"name": pname, "type": map_c_type(ptype, bindings_module)}
                    for pname, ptype in m["params"]
                ],
                "return_type": map_c_type(m["ret"], bindings_module),
                "doc": doc,
            }
    return list(out.values())


def _binding_class_name(pxd_class_key: str) -> str:
    return pxd_class_key[1:] if pxd_class_key.startswith("C") else pxd_class_key


def extract_errors(spec_mod) -> list[dict]:
    return [
        {"name": cls.__name__, "code": getattr(cls, "code", "service_error")}
        for cls in getattr(spec_mod, "ERRORS", [])
    ]


def bound_types_from_pxd(api: dict, bindings_module) -> list[type]:
    """Binding classes whose pxd entry exists AND that declare a
    _threading marker — i.e., types returned across the wrapper surface."""
    out = []
    for key, info in api["classes"].items():
        kls = getattr(bindings_module, _binding_class_name(key), None)
        if kls is not None and hasattr(kls, "_threading"):
            out.append(kls)
    return out
