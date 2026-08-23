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


def extract_wrapper(cls, hide=()) -> dict:
    """
    Reflect the live Python surface across the fake_library MRO chain:
    every public method and property the bindings actually expose, with
    leaf definitions winning over inherited ones. This - not the pxd -
    is the contract users program against; the pxd only feeds type
    policies elsewhere. `hide` drops names from the emitted surface.
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

    return {
        "name": cls.__qualname__,
        "module": cls.__module__,
        "bases": [f"{b.__module__}.{b.__qualname__}" for b in cls.__bases__ if b is not object],
        "threading": getattr(cls, "_threading", "affine"),
        "methods": methods,
    }


def _reader_method(name: str, val) -> dict:
    """Protocol dict for a readable attribute: a zero-arg read. Setters
    are not surfaced yet."""
    ret = "Any"
    doc = ""
    fget = getattr(val, "fget", None)
    if fget is not None:
        try:
            ret = _annotation_name(inspect.signature(fget).return_annotation)
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
