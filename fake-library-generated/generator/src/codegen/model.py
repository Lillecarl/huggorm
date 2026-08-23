"""
Introspect IDL classes into plain protocol dicts.

No I/O, no ast — pure reflection. The dict shape is the contract
between the IDL (spec.py) and the emitter (emitter.py).
"""

import inspect
from typing import Any, get_type_hints


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


def extract_service(cls) -> dict:
    own = {
        name: val
        for name, val in cls.__dict__.items()
        if not name.startswith("_") and callable(val)
    }
    marked = any(getattr(v, "_is_rpc", False) for v in own.values())
    if marked:
        own = {n: v for n, v in own.items() if getattr(v, "_is_rpc", False)}
    return {
        "service": cls.__qualname__,
        "module": cls.__module__,
        "bases": [f"{b.__module__}.{b.__qualname__}" for b in cls.__bases__ if b is not object],
        "threading": getattr(cls, "_threading", "affine"),
        "methods": [extract_method(f) for f in own.values()],
    }


def extract_errors(spec_mod) -> list[dict]:
    return [
        {"name": cls.__name__, "code": getattr(cls, "code", "service_error")}
        for cls in getattr(spec_mod, "ERRORS", [])
    ]


def collect_bound_types(service_classes) -> dict:
    """
    Find returned types that carry a _threading marker in the binding layer.

    Returns {class: threading} — insertion-ordered, deduplicated. A type
    participates only if it is defined in a fake_library module (i.e. it is
    a Cython wrapper) and declares _threading explicitly.
    """
    found = {}
    for cls in service_classes:
        for name, val in cls.__dict__.items():
            if not callable(val) or name.startswith("_"):
                continue
            try:
                hints = get_type_hints(val)
            except Exception:
                continue
            ret = hints.get("return")
            if ret is None or not hasattr(ret, "_threading"):
                continue
            if not getattr(ret, "__module__", "").startswith("fake_library"):
                raise ValueError(
                    f"{cls.__qualname__}.{name}: return type {ret.__name__} has "
                    "a _threading marker but is not from the bindings"
                )
            found.setdefault(ret, ret._threading)
    return found
