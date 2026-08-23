"""
Emit ast trees from protocol dicts.

Pure tree building — no I/O, no imports of the spec. Everything the
emitter needs arrives in the dict produced by model.extract_wrapper.
"""

import ast

RUNNER_BY_THREADING = {
    "affine": "AffineRunner",
    "pool": "PoolRunner",
}


def _ann(type_str: str, context: str) -> ast.expr:
    """Parse a type string into an annotation node. Strict: bad type strings fail loudly."""
    try:
        return ast.parse(type_str, mode="eval").body
    except SyntaxError as e:
        raise ValueError(f"unparseable annotation {type_str!r} on {context}") from e


def returned_module(proto: dict) -> ast.Module:
    """
    Emit Async<Bound> for a returned value type (e.g. Poop).

    Constructed with (obj, runner): the object was already produced on
    the producer's thread; attach_runner picks the right execution
    strategy from the type's declared policy.
    """
    svc = proto["name"]
    policy = proto["threading"]

    mod = ast.Module(body=[], type_ignores=[])
    used = {m["return_type"] for m in proto["methods"]} | {p["type"] for m in proto["methods"] for p in m["params"]}
    if "Any" in used:
        mod.body.append(ast.ImportFrom(module="typing", names=[ast.alias(name="Any")], level=0))
    mod.body.append(
        ast.Expr(
            value=ast.Constant(
                value=(
                    f"Generated async wrapper for returned type {svc} "
                    f"(threading: {policy}) - do not edit."
                )
            )
        )
    )
    mod.body.append(
        ast.ImportFrom(module="_runtime", names=[ast.alias(name="attach_runner")], level=1)
    )

    cls = ast.ClassDef(name=f"Async{svc}", bases=[], keywords=[], body=[], decorator_list=[])
    cls.body.append(
        ast.Expr(
            value=ast.Constant(
                value=(
                    f"Async handle over a {svc} produced by another wrapper. "
                    f"Policy '{policy}': "
                    + (
                        "operations run on the producer's thread."
                        if policy == "affine"
                        else "operations may run on any pool thread."
                    )
                )
            )
        )
    )
    cls.body.append(
        ast.FunctionDef(
            name="__init__",
            args=ast.arguments(
                posonlyargs=[],
                args=[ast.arg(arg="self"), ast.arg(arg="obj"), ast.arg(arg="runner")],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=[],
                kwarg=None,
                defaults=[],
            ),
            body=[
                ast.Assign(
                    targets=[ast.Attribute(value=ast.Name(id="self"), attr="_runner")],
                    value=ast.Call(
                        func=ast.Name(id="attach_runner"),
                        args=[
                            ast.Name(id="obj"),
                            ast.Name(id="runner"),
                            ast.Constant(value=policy),
                        ],
                        keywords=[],
                    ),
                )
            ],
            decorator_list=[],
            returns=None,
            type_params=[],
        )
    )

    mod.body.append(cls)
    _append_methods_and_aclose(cls, proto, svc)
    ast.fix_missing_locations(mod)
    return mod


def _append_methods_and_aclose(cls: ast.ClassDef, proto: dict, svc: str):
    for m in proto["methods"]:
        params = [ast.arg(arg="self")] + [
            ast.arg(arg=p["name"], annotation=_ann(p["type"], f"{svc}.{m['name']}:{p['name']}"))
            for p in m["params"]
        ]
        body: list[ast.stmt] = []
        if m["doc"]:
            body.append(ast.Expr(value=ast.Constant(value=m["doc"])))
        body.append(_hop_return(m["name"], m["params"]))
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
                returns=_ann(m["return_type"], f"{svc}.{m['name']}"),
                type_params=[],
            )
        )
    cls.body.append(_aclose_method())


def _hop_call(method_name: str, params: list[dict]) -> ast.Call:
    return ast.Call(
        func=ast.Attribute(
            value=ast.Attribute(value=ast.Name(id="self"), attr="_runner"),
            attr="call",
        ),
        args=[
            ast.Constant(value=method_name),
            ast.List(elts=[ast.Name(id=p["name"]) for p in params]),
        ],
        keywords=[],
    )


def _hop_return(method_name: str, params: list[dict]) -> ast.Return:
    return ast.Return(value=ast.Await(value=_hop_call(method_name, params)))


def wrapper_module(proto: dict, bound_policies: dict[str, str] | None = None) -> ast.Module:
    """Emit Async<Svc>. bound_policies maps returned-type names to their
    declared threading policy; those methods adopt the produced object
    into an attached runner instead of returning it raw."""
    svc = proto["name"]
    bound_policies = bound_policies or {}
    runner = RUNNER_BY_THREADING[proto["threading"]]

    mod = ast.Module(body=[], type_ignores=[])

    used_types = {p["type"] for m in proto["methods"] for p in m["params"]}
    used_types |= {m["return_type"] for m in proto["methods"]}
    used_types.add("None")  # aclose
    if "Any" in used_types:
        mod.body.append(ast.ImportFrom(module="typing", names=[ast.alias(name="Any")], level=0))
    bound_used = {m["return_type"] for m in proto["methods"] if m["return_type"] in bound_policies}
    for name in sorted(bound_used):
        mod.body.append(
            ast.ImportFrom(
                module=f"async_{name.lower()}",
                names=[ast.alias(name=f"Async{name}")],
                level=1,
            )
        )

    mod.body.append(
        ast.Expr(
            value=ast.Constant(
                value=(
                    f"Generated async wrapper for {svc} "
                    f"(threading: {proto['threading']}) - do not edit. "
                    f"Built via ast at Nix build time."
                )
            )
        )
    )
    mod.body.append(ast.ImportFrom(module="fake_library", names=[ast.alias(name=svc)], level=0))
    mod.body.append(
        ast.ImportFrom(module="_runtime", names=[ast.alias(name=runner)], level=1)
    )

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
                    f"Async in-process wrapper over {svc}. The object is "
                    f"constructed lazily on its runner thread."
                )
            )
        )
    )

    init_kwargs = []
    if proto["threading"] == "affine":
        init_kwargs.append(ast.keyword(arg="name", value=ast.Constant(value=f"flg-affine-{svc}")))
    cls.body.append(
        ast.FunctionDef(
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
                                    keywords=[
                                        ast.keyword(arg=None, value=ast.Name(id="kwargs"))
                                    ],
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
    )

    for m in proto["methods"]:
        params = [ast.arg(arg="self")] + [
            ast.arg(arg=p["name"], annotation=_ann(p["type"], f"{svc}.{m['name']}:{p['name']}"))
            for p in m["params"]
        ]
        body: list[ast.stmt] = []
        if m["doc"]:
            body.append(ast.Expr(value=ast.Constant(value=m["doc"])))
        rt = m["return_type"]
        if rt in bound_policies:
            # Adopt the produced object instead of returning it raw.
            body.append(
                ast.Assign(
                    targets=[ast.Name(id="result")],
                    value=ast.Await(value=_hop_call(m["name"], m["params"])),
                )
            )
            body.append(
                ast.Return(
                    value=ast.Call(
                        func=ast.Name(id=f"Async{rt}"),
                        args=[ast.Name(id="result"), ast.Attribute(value=ast.Name(id="self"), attr="_runner")],
                        keywords=[],
                    )
                )
            )
        else:
            body.append(_hop_return(m["name"], m["params"]))
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
                returns=(
                    _ann(f"Async{rt}", f"{svc}.{m['name']}")
                    if rt in bound_policies
                    else _ann(rt, f"{svc}.{m['name']}")
                ),
                type_params=[],
            )
        )

    cls.body.append(_aclose_method())
    mod.body.append(cls)
    ast.fix_missing_locations(mod)
    return mod


def _aclose_method() -> ast.AsyncFunctionDef:
    return ast.AsyncFunctionDef(
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
        returns=ast.parse("None", mode="eval").body,
        type_params=[],
    )


def init_module(all_names: list[str]) -> ast.Module:
    mod = ast.Module(body=[], type_ignores=[])
    mod.body.append(
        ast.Expr(
            value=ast.Constant(
                value="Generated async wrappers - do not edit. Built via ast at Nix build time."
            )
        )
    )
    for name in all_names:
        fname = f"async_{name.lower()}"
        mod.body.append(ast.ImportFrom(module=fname, names=[ast.alias(name=f"Async{name}")], level=1))
    mod.body.append(
        ast.Assign(
            targets=[ast.Name(id="__all__")],
            value=ast.List(
                elts=[ast.Constant(value=f"Async{n}") for n in all_names]
            ),
        )
    )
    ast.fix_missing_locations(mod)
    return mod
