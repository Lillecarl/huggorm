#!/usr/bin/env python3
"""
AST-based codegen for fake_library_generated.

Do NOT emit Python by string concatenation. Build `ast` nodes and
call `ast.unparse` — this gives you syntax checking, proper handling
of annotations, and tooling-friendly output.

Invoked at Nix build time:
    python generator/generate.py --out ./fake_library_generated
"""

import argparse
import ast
import inspect
import pathlib
import sys
from typing import Any, get_type_hints


def _type_to_ast(type_str: str) -> ast.expr:
    """Parse a type string into an AST node via ast.parse."""
    try:
        # Handles `str`, `int`, `list[str]`, `Optional[int]`, etc.
        return ast.parse(type_str, mode="eval").body  # type: ignore[return-value]
    except Exception:
        return ast.Name(id="Any")


def _build_client_class(protocol: dict[str, Any]) -> ast.ClassDef:
    svc = protocol["service"]
    methods = protocol["methods"]

    # ClassDef: class <Svc>Client:
    cls = ast.ClassDef(
        name=f"{svc}Client",
        bases=[],
        keywords=[],
        body=[],
        decorator_list=[],
    )
    # Docstring
    cls.body.append(
        ast.Expr(value=ast.Constant(value=f"Client stub for {svc}. Each method does transport.call."))
    )

    # def __init__(self, transport):
    init = ast.FunctionDef(
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
    cls.body.append(init)

    for m in methods:
        name = m["name"]
        params: list[ast.arg] = [ast.arg(arg="self")]
        for p in m["params"]:
            ann = _type_to_ast(p["type"])
            params.append(ast.arg(arg=p["name"], annotation=ann))
        ret_ann = _type_to_ast(m["return_type"])

        # Body: [docstring] + return self.transport.call("name", [args])
        body: list[ast.stmt] = []
        if m.get("doc"):
            body.append(ast.Expr(value=ast.Constant(value=m["doc"])))
        call_args = [ast.Constant(value=name), ast.List(elts=[ast.Name(id=p["name"]) for p in m["params"]])]
        body.append(
            ast.Return(
                value=ast.Call(
                    func=ast.Attribute(
                        value=ast.Attribute(value=ast.Name(id="self"), attr="transport"),
                        attr="call",
                    ),
                    args=call_args,
                    keywords=[],
                )
            )
        )
        func_def = ast.FunctionDef(
            name=name,
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
        cls.body.append(func_def)

    if not methods:
        cls.body.append(ast.Pass())

    return cls


def generate_protocol(cls) -> dict[str, Any]:
    """Introspect a Python subclass of a Cython type."""
    own_methods = {
        name: val for name, val in cls.__dict__.items() if not name.startswith("_") and callable(val)
    }
    has_rpc = any(getattr(v, "_is_rpc", False) for v in own_methods.values())
    if has_rpc:
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
        methods.append({"name": name, "params": param_specs, "return_type": ret, "doc": inspect.getdoc(func) or ""})

    bases = [f"{b.__module__}.{b.__qualname__}" for b in cls.__bases__ if b is not object]
    return {"service": cls.__qualname__, "bases": bases, "methods": methods}


def build_module(protocol: dict[str, Any]) -> ast.Module:
    mod = ast.Module(body=[], type_ignores=[])
    # from __future__ import annotations not needed, but add header comment via docstring
    # Add: from typing import Any if needed
    has_any = any(p["type"] == "Any" or m["return_type"] == "Any" for m in protocol["methods"] for p in m["params"]) or any(
        m["return_type"] == "Any" for m in protocol["methods"]
    )
    if has_any:
        mod.body.append(ast.ImportFrom(module="typing", names=[ast.alias(name="Any")], level=0))
    mod.body.append(ast.Expr(value=ast.Constant(value=f"Auto-generated RPC stub for {protocol['service']}. Bases: {', '.join(protocol['bases'])}")))
    mod.body.append(_build_client_class(protocol))
    ast.fix_missing_locations(mod)
    return mod


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="output directory for fake_library_generated")
    parser.add_argument("--spec", default="spec", help="spec module name (default: spec)")
    args = parser.parse_args()

    # Ensure spec can be imported (spec.py is sibling of generator/)
    spec_dir = pathlib.Path(__file__).parent.parent
    if str(spec_dir) not in sys.path:
        sys.path.insert(0, str(spec_dir))

    import importlib

    spec_mod = importlib.import_module(args.spec)
    services = getattr(spec_mod, "SERVICES", [])
    if not services:
        print(f"No SERVICES found in {args.spec}", file=sys.stderr)
        sys.exit(1)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "__init__.py").write_text(
        '"""Generated RPC clients — do not edit. Built via ast at Nix build time."""\n'
    )

    # Track what we generated for __init__ re-exports
    exports = []
    for svc in services:
        proto = generate_protocol(svc)
        mod = build_module(proto)
        code = ast.unparse(mod)
        fname = f"{proto['service'].lower()}_client.py"
        # Use lowercased service name for filename; e.g. RemoteCat -> remotecat_client.py
        (out / fname).write_text(code + "\n")
        exports.append((proto["service"], fname))
        print(f"generated {fname} for {proto['service']}")

    # Write __init__.py re-exports for convenience
    init_lines = ['"""Generated RPC clients — do not edit. Built via ast at Nix build time."""', ""]
    for svc, fname in exports:
        modname = fname[:-3]
        init_lines.append(f"from .{modname} import {svc}Client")
    init_lines.append("")
    init_lines.append(f"__all__ = [{', '.join(repr(svc+'Client') for svc,_ in exports)}]")
    (out / "__init__.py").write_text("\n".join(init_lines) + "\n")

    # Also write a JSON manifest for introspection
    import json

    manifest = {svc.__qualname__: generate_protocol(svc) for svc in services}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote manifest with {len(services)} services to {out / 'manifest.json'}")

    # Ship the spec (IDL) inside the generated package so server-side
    # instances are importable wherever the clients are.
    import shutil

    spec_file = spec_dir / f"{args.spec}.py"
    shutil.copy(spec_file, out / "spec.py")
    print(f"Copied IDL spec into package: {out / 'spec.py'}")


if __name__ == "__main__":
    main()
