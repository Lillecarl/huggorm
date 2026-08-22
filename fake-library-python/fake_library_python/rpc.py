"""
RPC protocol codegen from Python subclasses of Cython types.

The Cython base (Cat/Dog/Animal) is only used for typing and for its
C++-backed fields/methods (e.g. `name`, `speak`, `legs`). The RPC layer
never touches C++ — it introspects the Python subclass, collects its
methods, and emits a protocol description that you can codegen stubs
or documentation from.

Both patterns coexist:
- `class Spider(Animal):` uses the PyAnimal trampoline, so C++ `describe(spider)`
  sees the override (the `Animal` path).
- `class RemoteCat(Cat):` is pure Python RPC (no C++ trampoline). It adds new
  methods like `greet` that you codegen for the wire. C++ never sees them,
  and it can't — the remote side may not even be C++.

Usage:
    from fake_library import Cat
    from fake_library_python.rpc import rpc, generate_protocol, generate_stub

    class MyCat(Cat):
        @rpc
        def greet(self, whom: str) -> str:
            return f"{self.speak()} to {whom}"

    proto = generate_protocol(MyCat)
    print(generate_stub(proto))
"""

import inspect
import json
from typing import Any, get_type_hints


def rpc(func):
    """Mark a method as part of the RPC protocol."""
    func._is_rpc = True  # type: ignore[attr-defined]
    return func


def _is_cython_base(cls) -> bool:
    # Cython extension types have __pyx_vtable__ or are from fake_library
    return getattr(cls, "__module__", "").startswith("fake_library")


def _base_names(cls) -> list[str]:
    bases = []
    for b in cls.__bases__:
        if b is object:
            continue
        bases.append(f"{b.__module__}.{b.__qualname__}")
    return bases


def generate_protocol(cls) -> dict[str, Any]:
    """
    Introspect a Python subclass of a Cython type and produce a protocol dict.

    Picks up:
    - Every method defined directly in `cls.__dict__` (overrides + new methods)
    - Filters to those marked with @rpc OR all public callables if none are marked
    - Captures signature, type hints, and docstring
    """
    if not isinstance(cls, type):
        raise TypeError("generate_protocol() expects a class")

    # Collect methods defined in the subclass itself
    own_methods = {
        name: val
        for name, val in cls.__dict__.items()
        if not name.startswith("_") and callable(val)
    }

    # If any method is @rpc-marked, only emit those; otherwise emit all own methods.
    # This lets you do explicit @rpc or implicit "every public method is RPC".
    has_rpc_marks = any(getattr(v, "_is_rpc", False) for v in own_methods.values())
    if has_rpc_marks:
        own_methods = {k: v for k, v in own_methods.items() if getattr(v, "_is_rpc", False)}

    methods = []
    try:
        hints_by_method: dict[str, dict] = {}
        # get_type_hints per-method handles forward refs; fall back to raw annotations
    except Exception:
        hints_by_method = {}

    for name, func in own_methods.items():
        try:
            sig = inspect.signature(func)
            # Drop `self`
            params = list(sig.parameters.values())[1:]
            # Resolve hints for this function
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
                    # stringify type for protocol
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
        except Exception as e:
            methods.append({"name": name, "error": str(e)})

    # Also note which Cython fields/methods are inherited (for completeness)
    inherited = []
    for base in cls.__mro__[1:]:
        if _is_cython_base(base):
            inherited.append(f"{base.__module__}.{base.__qualname__}")

    return {
        "service": cls.__qualname__,
        "module": cls.__module__,
        "bases": _base_names(cls),
        "cython_bases": inherited,
        "methods": methods,
    }


def _type_to_ast(type_str: str):
    """Parse a type string into an AST node — keeps annotations as proper nodes."""
    import ast

    try:
        return ast.parse(type_str, mode="eval").body  # type: ignore[return-value]
    except Exception:
        return ast.Name(id="Any")


def _build_client_class_ast(protocol: dict[str, Any]):
    import ast

    svc = protocol["service"]
    methods = protocol["methods"]

    cls = ast.ClassDef(
        name=f"{svc}Client",
        bases=[],
        keywords=[],
        body=[ast.Expr(value=ast.Constant(value=f"Client stub for {svc}. Each method does transport.call."))],
        decorator_list=[],
    )
    # def __init__(self, transport):
    cls.body.append(
        ast.FunctionDef(
            name="__init__",
            args=ast.arguments(
                posonlyargs=[],
                args=[ast.arg(arg="self"), ast.arg(arg="transport")],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=[],
                kwarg=None,
                defaults=[],
            ),
            body=[
                ast.Assign(
                    targets=[ast.Attribute(value=ast.Name(id="self"), attr="transport")],
                    value=ast.Name(id="transport"),
                )
            ],
            decorator_list=[],
            returns=None,
            type_params=[],
        )
    )
    for m in methods:
        params = [ast.arg(arg="self")] + [
            ast.arg(arg=p["name"], annotation=_type_to_ast(p["type"])) for p in m["params"]
        ]
        ret_ann = _type_to_ast(m["return_type"])
        body: list[Any] = []
        if m.get("doc"):
            body.append(ast.Expr(value=ast.Constant(value=m["doc"])))
        body.append(
            ast.Return(
                value=ast.Call(
                    func=ast.Attribute(
                        value=ast.Attribute(value=ast.Name(id="self"), attr="transport"),
                        attr="call",
                    ),
                    args=[
                        ast.Constant(value=m["name"]),
                        ast.List(elts=[ast.Name(id=p["name"]) for p in m["params"]]),
                    ],
                    keywords=[],
                )
            )
        )
        cls.body.append(
            ast.FunctionDef(
                name=m["name"],
                args=ast.arguments(
                    posonlyargs=[],
                    args=params,
                    vararg=None,
                    kwonlyargs=[],
                    kw_defaults=[],
                    kwarg=None,
                    defaults=[],
                ),
                body=body,
                decorator_list=[],
                returns=ret_ann,
                type_params=[],
            )
        )
    if not methods:
        cls.body.append(ast.Pass())
    return cls


def generate_stub(protocol: dict[str, Any]) -> str:
    """Emit a Python RPC stub via `ast` — syntax-checked, not string-concatenated."""
    import ast

    mod = ast.Module(body=[], type_ignores=[])
    has_any = any(p["type"] == "Any" or m["return_type"] == "Any" for m in protocol["methods"] for p in m["params"]) or any(
        m["return_type"] == "Any" for m in protocol["methods"]
    )
    if has_any:
        mod.body.append(ast.ImportFrom(module="typing", names=[ast.alias(name="Any")], level=0))
    mod.body.append(ast.Expr(value=ast.Constant(value=f"Auto-generated RPC stub for {protocol['service']}. Bases: {', '.join(protocol['bases'])}")))
    mod.body.append(_build_client_class_ast(protocol))
    ast.fix_missing_locations(mod)
    return ast.unparse(mod)


def print_protocol(cls) -> None:
    proto = generate_protocol(cls)
    print(json.dumps(proto, indent=2))
    print("\n--- stub ---\n")
    print(generate_stub(proto))
