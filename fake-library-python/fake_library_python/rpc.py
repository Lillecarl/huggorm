"""
Introspection helpers for the IDL (spec.py in fake-library-generated).

The Cython base (Cat/Dog/Animal) provides typing and C++-backed
fields/methods. The IDL subclasses it and marks methods with @rpc;
this module collects them into a protocol dict.

Codegen lives in fake-library-generated/generator/generate.py and uses
`ast` — this module only inspects classes.

RPC/wire concerns are deferred; right now the generated layer emits
async in-process wrappers with thread-affinity policies.
"""

import inspect
from typing import Any, get_type_hints


def rpc(func):
    """Mark a method as exposed by generated wrappers."""
    func._is_rpc = True  # type: ignore[attr-defined]
    return func


def _is_cython_base(cls) -> bool:
    return getattr(cls, "__module__", "").startswith("fake_library")


def _base_names(cls) -> list[str]:
    return [f"{b.__module__}.{b.__qualname__}" for b in cls.__bases__ if b is not object]


def generate_protocol(cls) -> dict[str, Any]:
    """
    Introspect an IDL class into a protocol dict.

    - Methods marked @rpc, or all public own methods if none are marked
    - Captures signature, resolved type hints, docstring
    - Includes threading model from @rpc_service(threading=...)
    """
    if not isinstance(cls, type):
        raise TypeError("generate_protocol() expects a class")

    own_methods = {
        name: val
        for name, val in cls.__dict__.items()
        if not name.startswith("_") and callable(val)
    }

    has_rpc_marks = any(getattr(v, "_is_rpc", False) for v in own_methods.values())
    if has_rpc_marks:
        own_methods = {k: v for k, v in own_methods.items() if getattr(v, "_is_rpc", False)}

    methods = []
    for name, func in own_methods.items():
        sig = inspect.signature(func)
        params = list(sig.parameters.values())[1:]
        try:
            hints = get_type_hints(func)
        except Exception:
            hints = getattr(func, "__annotations__", {})
        param_specs = []
        for p in params:
            ann = hints.get(p.name, p.annotation)
            if ann is inspect.Signature.empty:
                ann = "Any"
            else:
                ann = getattr(ann, "__name__", str(ann))
            param_specs.append({"name": p.name, "type": ann})
        ret = hints.get("return", sig.return_annotation)
        if ret is inspect.Signature.empty:
            ret = "Any"
        else:
            ret = getattr(ret, "__name__", str(ret))
        methods.append(
            {
                "name": name,
                "params": param_specs,
                "return_type": ret,
                "doc": inspect.getdoc(func) or "",
            }
        )

    inherited = []
    for base in cls.__mro__[1:]:
        if _is_cython_base(base):
            inherited.append(f"{base.__module__}.{base.__qualname__}")

    return {
        "service": cls.__qualname__,
        "module": cls.__module__,
        "bases": _base_names(cls),
        "cython_bases": inherited,
        "threading": getattr(cls, "_threading", "affine"),
        "methods": methods,
    }
