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

# Annotation atoms that never need an import. Everything else must be
# imported from fake_library, or get_type_hints raises NameError -
# invisible on Python 3.14 (PEP 649 lazy annotations), fatal below.
_BUILTIN_TYPES = {"None", "Any", "str", "int", "float", "bool", "bytes", "object"}


def _names_in(type_str: str) -> set[str]:
    """Every named type inside one annotation string."""
    node = ast.parse(type_str, mode="eval").body
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _param_ann(type_str: str, async_types: set[str]) -> str:
    """The annotation a parameter really accepts.

    unwrap_arg takes either side: the sync binding object or the async
    wrapper over it, which contributes its target. Annotating the sync
    type alone was a lie - in-process callers pass wrappers (that is
    the whole surface), the RPC server passes sync objects. Say both."""
    return f"{type_str} | Async{type_str}" if type_str in async_types else type_str


def _emitted_annotations(proto: dict, async_types: set[str],
                         bound_policies: dict[str, str]) -> list[str]:
    """The annotation strings the emitter will actually write. Import
    collection reads THIS, not the raw protocol types: a return type
    that gets adopted is written as AsyncX and must not drag the sync X
    into the module as an unused import."""
    out = []
    for m in proto["methods"]:
        out += [_param_ann(p["type"], async_types) for p in m["params"]]
        rt = m["return_type"]
        out.append(f"Async{rt}" if rt in bound_policies else rt)
    return out


def _annotation_names(annotations: list[str]) -> set[str]:
    out: set[str] = set()
    for a in annotations:
        out |= _names_in(a)
    return out


def _fake_library_import(names: set[str]) -> ast.ImportFrom | None:
    """Import the SYNC binding types an emitted module annotates with.
    Async* names are excluded: those come from sibling modules."""
    usable = sorted(
        n for n in names if n not in _BUILTIN_TYPES and not n.startswith("Async")
    )
    if not usable:
        return None
    return ast.ImportFrom(
        module="fake_library", names=[ast.alias(name=n) for n in usable], level=0
    )


def _sibling_imports(names: set[str]) -> list[ast.ImportFrom]:
    """`from .async_x import AsyncX` for every generated wrapper an
    emitted module annotates with - adopted returns and wrapper-typed
    parameters alike."""
    return [
        ast.ImportFrom(
            module=f"async_{n.removeprefix('Async').lower()}",
            names=[ast.alias(name=n)],
            level=1,
        )
        for n in sorted(n for n in names if n.startswith("Async"))
    ]


def _ann(type_str: str, context: str) -> ast.expr:
    """Parse a type string into an annotation node. Strict: bad type strings fail loudly."""
    try:
        return ast.parse(type_str, mode="eval").body
    except SyntaxError as e:
        raise ValueError(f"unparseable annotation {type_str!r} on {context}") from e


def returned_module(proto: dict, async_types: set[str] | None = None) -> ast.Module:
    """
    Emit Async<Bound> for a returned value type (e.g. Poop).

    Constructed with (obj, runner): the object was already produced on
    the producer's thread; attach_runner picks the right execution
    strategy from the type's declared policy.
    """
    svc = proto["name"]
    policy = proto["threading"]
    policy_wire = proto["wire"]
    async_types = async_types or set()

    mod = ast.Module(body=[], type_ignores=[])
    annotations = _emitted_annotations(proto, async_types, {})
    used = _annotation_names(annotations)
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
    mod.body.extend(_sibling_imports(used))
    ann_import = _fake_library_import(used)
    if ann_import is not None:
        mod.body.append(ann_import)

    cls = ast.ClassDef(name=f"Async{svc}", bases=[], keywords=[], body=[], decorator_list=[])
    # Docstring FIRST: a string preceded by any other statement is a
    # dead expression, not __doc__.
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
    cls.body.append(ast.Assign(
        targets=[ast.Name(id="_wire")],
        value=ast.Constant(value=policy_wire),
    ))
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
    _append_methods_and_aclose(cls, proto, svc, async_types)
    ast.fix_missing_locations(mod)
    return mod


def _append_methods_and_aclose(cls: ast.ClassDef, proto: dict, svc: str,
                               async_types: set[str]):
    for m in proto["methods"]:
        params = [ast.arg(arg="self")] + [
            ast.arg(arg=p["name"],
                    annotation=_ann(_param_ann(p["type"], async_types),
                                    f"{svc}.{m['name']}:{p['name']}"))
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


def wrapper_module(proto: dict, bound_policies: dict[str, str] | None = None,
                   async_types: set[str] | None = None) -> ast.Module:
    """Emit Async<Svc>. bound_policies maps returned-type names to their
    declared threading policy; those methods adopt the produced object
    into an attached runner instead of returning it raw. async_types is
    every generated wrapper name, used to widen parameter annotations."""
    svc = proto["name"]
    bound_policies = bound_policies or {}
    async_types = async_types or set()
    runner = RUNNER_BY_THREADING[proto["threading"]]

    mod = ast.Module(body=[], type_ignores=[])

    # Docstring FIRST: the bound-type imports used to be emitted ahead
    # of it, which demoted it to a dead expression and left the module
    # with no __doc__.
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

    annotations = _emitted_annotations(proto, async_types, bound_policies)
    used_types = _annotation_names(annotations) | {"None"}  # None: aclose
    if "Any" in used_types:
        mod.body.append(ast.ImportFrom(module="typing", names=[ast.alias(name="Any")], level=0))
    # Never import our own class from ourselves.
    mod.body.extend(_sibling_imports(used_types - {f"Async{svc}"}))

    mod.body.append(ast.ImportFrom(module="fake_library", names=[ast.alias(name=svc)], level=0))
    ann_import = _fake_library_import(used_types - {svc})
    if ann_import is not None:
        mod.body.append(ann_import)
    mod.body.append(
        ast.ImportFrom(module="_runtime", names=[ast.alias(name=runner)], level=1)
    )
    mod.body.append(
        ast.ImportFrom(module="_runtime", names=[ast.alias(name="unwrap_arg")], level=1)
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
    cls.body.append(ast.Assign(
        targets=[ast.Name(id="_wire")],
        value=ast.Constant(value=proto["wire"]),
    ))

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
                                    args=[
                                        # Replay constructor args through
                                        # unwrap_arg: a wrapper argument
                                        # contributes its target object.
                                        ast.Starred(
                                            value=ast.ListComp(
                                                elt=ast.Call(
                                                    func=ast.Name(id="unwrap_arg"),
                                                    args=[ast.Name(id="a")],
                                                    keywords=[],
                                                ),
                                                generators=[
                                                    ast.comprehension(
                                                        target=ast.Name(id="a"),
                                                        iter=ast.Name(id="args"),
                                                        ifs=[],
                                                        is_async=0,
                                                    )
                                                ],
                                            )
                                        )
                                    ],
                                    keywords=[
                                        # Replay constructor kwargs through
                                        # unwrap_arg too: a wrapper passed
                                        # as kwarg contributes its target
                                        # object, not the async shell.
                                        ast.keyword(
                                            arg=None,
                                            value=ast.DictComp(
                                                key=ast.Name(id="k"),
                                                value=ast.Call(
                                                    func=ast.Name(id="unwrap_arg"),
                                                    args=[ast.Name(id="v")],
                                                    keywords=[],
                                                ),
                                                generators=[
                                                    ast.comprehension(
                                                        target=ast.Tuple(
                                                            elts=[ast.Name(id="k"), ast.Name(id="v")],
                                                            ctx=ast.Load(),
                                                        ),
                                                        iter=ast.Call(
                                                            func=ast.Attribute(
                                                                value=ast.Name(id="kwargs"),
                                                                attr="items",
                                                            ),
                                                            args=[],
                                                            keywords=[],
                                                        ),
                                                        ifs=[],
                                                        is_async=0,
                                                    )
                                                ],
                                            ),
                                        )
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
            ast.arg(arg=p["name"],
                    annotation=_ann(_param_ann(p["type"], async_types),
                                    f"{svc}.{m['name']}:{p['name']}"))
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
