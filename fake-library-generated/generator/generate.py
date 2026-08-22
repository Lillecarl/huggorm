#!/usr/bin/env python3
"""
AST-based codegen for fake_library_generated.

Do NOT emit Python by string concatenation. Build `ast` nodes and
call `ast.unparse` — this gives you syntax checking, proper handling
of annotations, and tooling-friendly output.

Emits ASYNC IN-PROCESS wrappers over the spec services:
    class AsyncRemoteCat:
        def __init__(self, *args, **kwargs):
            self._runner = AffineRunner(lambda *a, **kw: RemoteCat(*a, **kw), name=...)
        async def greet(self, whom: str) -> str:
            return await self._runner.call('greet', [whom])

Threading model comes from the IDL (@rpc_service(threading=...)):
- affine: object constructed on + pinned to one dedicated thread
- pool:   object runs on a shared thread pool

RPC/wire codegen will layer on top of this later.

Invoked at Nix build time:
    python generator/generate.py --out ./fake_library_generated
"""

import argparse
import ast
import importlib
import json
import inspect
import pathlib
import shutil
import sys
from typing import Any, get_type_hints


def _type_to_ast(type_str: str) -> ast.expr:
    """Parse a type string into an AST node via ast.parse."""
    try:
        return ast.parse(type_str, mode="eval").body  # type: ignore[return-value]
    except Exception:
        return ast.Name(id="Any")


def _runner_class(threading_model: str) -> str:
    if threading_model == "pool":
        return "PoolRunner"
    return "AffineRunner"


def _build_async_wrapper(protocol: dict[str, Any]) -> ast.ClassDef:
    svc = protocol["service"]
    runner = _runner_class(protocol["threading"])

    cls = ast.ClassDef(
        name=f"Async{svc}",
        bases=[],
        keywords=[],
        body=[],
        decorator_list=[],
    )
    cls.body.append(
        ast.Expr(
            value=ast.Constant(
                value=(
                    f"Async in-process wrapper over {svc} "
                    f"(threading: {protocol['threading']}). "
                    f"Object is constructed lazily on its runner thread."
                )
            )
        )
    )

    # def __init__(self, *args, **kwargs):
    init_kwargs = []
    if protocol["threading"] == "affine":
        init_kwargs.append(ast.keyword(arg="name", value=ast.Constant(value=f"flg-affine-{svc}")))
    init = ast.FunctionDef(
        name="__init__",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="self")],
            vararg=ast.arg(arg="args"),
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=ast.arg(arg="kwargs"),
            defaults=[],
        ),
        body=[
            ast.Assign(
                targets=[ast.Attribute(value=ast.Name(id="self"), attr="_runner")],
                value=ast.Call(
                    func=ast.Name(id=runner),
                    args=[
                        # Zero-arg lambda closing over this __init__'s
                        # args/kwargs: lambda: RemoteCat(*args, **kwargs)
                        ast.Lambda(
                            args=ast.arguments(
                                posonlyargs=[],
                                args=[],
                                vararg=None,
                                kwonlyargs=[],
                                kw_defaults=[],
                                kwarg=None,
                                defaults=[],
                            ),
                            body=ast.Call(
                                func=ast.Name(id=svc),
                                args=[ast.Starred(value=ast.Name(id="args"))],
                                keywords=[ast.keyword(arg=None, value=ast.Name(id="kwargs"))],
                            ),
                        )
                    ],
                    keywords=init_kwargs,
                ),
            )
        ],
        decorator_list=[],
        returns=None,
        type_params=[],
    )
    cls.body.append(init)

    # async def <method>(self, ...) -> ret:
    #     """doc"""
    #     return await self._runner.call('<method>', [args])
    for m in protocol["methods"]:
        params = [ast.arg(arg="self")] + [
            ast.arg(arg=p["name"], annotation=_type_to_ast(p["type"])) for p in m["params"]
        ]
        ret_ann = _type_to_ast(m["return_type"])
        body: list[ast.stmt] = []
        if m.get("doc"):
            body.append(ast.Expr(value=ast.Constant(value=m["doc"])))
        body.append(
            ast.Return(
                value=ast.Await(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Attribute(value=ast.Name(id="self"), attr="_runner"),
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
        )
        cls.body.append(
            ast.AsyncFunctionDef(
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

    # async def aclose(self) -> None:
    #     await self._runner.aclose()
    cls.body.append(
        ast.AsyncFunctionDef(
            name="aclose",
            args=ast.arguments(
                posonlyargs=[],
                args=[ast.arg(arg="self", annotation=None)],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=[],
                kwarg=None,
                defaults=[],
            ),
            body=[
                ast.Expr(
                    value=ast.Await(
                        value=ast.Call(
                            func=ast.Attribute(
                                value=ast.Attribute(value=ast.Name(id="self"), attr="_runner"),
                                attr="aclose",
                            ),
                            args=[],
                            keywords=[],
                        )
                    )
                )
            ],
            decorator_list=[],
            returns=_type_to_ast("None"),
            type_params=[],
        )
    )

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
    threading_model = getattr(cls, "_threading", "affine")
    return {"service": cls.__qualname__, "bases": bases, "threading": threading_model, "methods": methods}


def build_module(protocol: dict[str, Any]) -> ast.Module:
    mod = ast.Module(body=[], type_ignores=[])

    has_any = any(
        p["type"] == "Any" or m["return_type"] == "Any"
        for m in protocol["methods"]
        for p in m["params"]
    ) or any(m["return_type"] == "Any" for m in protocol["methods"])
    if has_any:
        mod.body.append(ast.ImportFrom(module="typing", names=[ast.alias(name="Any")], level=0))

    mod.body.append(
        ast.Expr(
            value=ast.Constant(
                value=f"Generated async wrapper for {protocol['service']} — do not edit. Built via ast at Nix build time."
            )
        )
    )
    mod.body.append(ast.ImportFrom(module="spec", names=[ast.alias(name=protocol["service"])], level=1))
    mod.body.append(
        ast.ImportFrom(
            module="_runtime",
            names=[ast.alias(name=_runner_class(protocol["threading"]))],
            level=1,
        )
    )
    mod.body.append(_build_async_wrapper(protocol))
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

    spec_mod = importlib.import_module(args.spec)
    services = getattr(spec_mod, "SERVICES", [])
    if not services:
        print(f"No SERVICES found in {args.spec}", file=sys.stderr)
        sys.exit(1)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    exports = []
    for svc in services:
        proto = generate_protocol(svc)
        mod = build_module(proto)
        code = ast.unparse(mod)
        fname = f"async_{proto['service'].lower()}.py"
        (out / fname).write_text(code + "\n")
        exports.append((proto["service"], fname))
        print(f"generated {fname} for {proto['service']} ({proto['threading']})")

    # __init__.py re-exports
    init_lines = [
        '"""Generated async wrappers — do not edit. Built via ast at Nix build time."""',
        "",
    ]
    for svc, fname in exports:
        modname = fname[:-3]
        init_lines.append(f"from .{modname} import Async{svc}")
    init_lines.append("")
    init_lines.append(f"__all__ = [{', '.join(repr('Async' + svc) for svc, _ in exports)}]")
    (out / "__init__.py").write_text("\n".join(init_lines) + "\n")

    # JSON manifest for introspection/tooling
    errors = [
        {"name": cls.__name__, "code": getattr(cls, "code", "service_error")}
        for cls in getattr(spec_mod, "ERRORS", [])
    ]
    manifest = {
        svc.__qualname__: generate_protocol(svc) for svc in services
    }
    manifest_doc = {"services": manifest, "errors": errors}
    (out / "manifest.json").write_text(json.dumps(manifest_doc, indent=2) + "\n")
    print(f"Wrote manifest ({len(services)} services, {len(errors)} errors) to {out / 'manifest.json'}")

    # Ship the spec (IDL) and runtime inside the generated package so it is
    # fully self-contained wherever Nix puts it.
    spec_file = spec_dir / f"{args.spec}.py"
    shutil.copy(spec_file, out / "spec.py")
    runtime_file = pathlib.Path(__file__).parent / "runtime.py"
    shutil.copy(runtime_file, out / "_runtime.py")
    init_pkg = '"""Generated package: async wrappers + IDL spec + runtime."""\n'
    print(f"Copied IDL spec and runtime into package: {out}")


if __name__ == "__main__":
    main()
