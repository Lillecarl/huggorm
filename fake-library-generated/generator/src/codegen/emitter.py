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
_BUILTIN_TYPES = {"None", "Any", "str", "int", "float", "bool", "bytes",
                  "object", "dict", "list", "tuple", "set"}


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


def _ctor_args(proto: dict, async_types: set[str]) -> ast.arguments:
    """Typed __init__ parameters from the declared constructor.

    This used to be `*args, **kwargs` forwarded blind, because nothing
    knew the constructor's shape. It does now (see model.constructor_
    signature), so the wrapper states it: wrong arity fails at the call
    site instead of inside a lazy factory on some worker thread, and a
    typechecker can see it."""
    args = [ast.arg(arg="self")]
    defaults = []
    for p in proto["ctor"]:
        ann = _param_ann(p["type"], async_types)
        if p["optional"]:
            # Spell the None out. `output: str = None` is implicit
            # Optional, which strict typecheckers reject and which
            # misdescribes the default the emitter itself writes.
            ann += " | None"
        args.append(ast.arg(
            arg=p["name"],
            annotation=_ann(ann, f"{proto['name']}.__init__:{p['name']}")))
        if p["optional"]:
            defaults.append(ast.Constant(value=None))
        elif defaults:
            # sorted-by-arity overloads cannot produce this, but a future
            # explicit declaration could.
            raise ValueError(
                f"{proto['name']}.__init__: required parameter {p['name']!r} "
                f"follows an optional one")
    return ast.arguments(posonlyargs=[], args=args, vararg=None, kwonlyargs=[],
                         kw_defaults=[], kwarg=None, defaults=defaults)


def _emitted_annotations(proto: dict, async_types: set[str],
                         bound_policies: dict[str, str]) -> list[str]:
    """The annotation strings the emitter will actually write. Import
    collection reads THIS, not the raw protocol types: a return type
    that gets adopted is written as AsyncX and must not drag the sync X
    into the module as an unused import."""
    out = [_param_ann(p["type"], async_types) for p in proto.get("ctor", ())]
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


def _append_hop_method(cls: ast.ClassDef, proto: dict, m: dict, svc: str,
                       async_types: set[str], bound_policies: dict[str, str]):
    """One `async def` that hops to the runner. Shared by the abstract
    base and its subclasses: the body is identical either way, which is
    exactly why a base can carry it - the runner comes from whichever
    __init__ ran."""
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
        body.append(ast.Assign(
            targets=[ast.Name(id="result")],
            value=ast.Await(value=_hop_call(m["name"], m["params"]))))
        body.append(ast.Return(value=ast.Call(
            func=ast.Name(id=f"Async{rt}"),
            args=[ast.Name(id="result"),
                  ast.Attribute(value=ast.Name(id="self"), attr="_runner")],
            keywords=[])))
    else:
        body.append(_hop_return(m["name"], m["params"]))
    cls.body.append(ast.AsyncFunctionDef(
        name=m["name"],
        args=ast.arguments(posonlyargs=[], args=params, vararg=None,
                           kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]),
        body=body,
        decorator_list=[],
        returns=(_ann(f"Async{rt}", f"{svc}.{m['name']}") if rt in bound_policies
                 else _ann(rt, f"{svc}.{m['name']}")),
        type_params=[]))


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
    if proto.get("async_base"):
        used_types.add(f"Async{proto['async_base']}")
    mod.body.extend(_sibling_imports(used_types - {f"Async{svc}"}))

    # An abstract base constructs nothing, so it imports neither the
    # sync target nor a runner class. It still annotates with the target
    # if a method mentions it, which is why the subtraction below is
    # conditional too.
    constructs = not proto["abstract"]
    if constructs:
        mod.body.append(ast.ImportFrom(
            module="fake_library", names=[ast.alias(name=svc)], level=0))
    ann_import = _fake_library_import(used_types - ({svc} if constructs else set()))
    if ann_import is not None:
        mod.body.append(ann_import)
    if constructs:
        mod.body.append(
            ast.ImportFrom(module="_runtime", names=[ast.alias(name=runner)], level=1)
        )
    if proto["ctor"] and constructs:
        # Only the factory calls it, so a constructor taking nothing
        # leaves the import unused - which the smoke gate rejects.
        mod.body.append(
            ast.ImportFrom(module="_runtime", names=[ast.alias(name="unwrap_arg")], level=1)
        )

    base = proto.get("async_base")
    cls = ast.ClassDef(
        name=f"Async{svc}",
        bases=[ast.Name(id=f"Async{base}")] if base else [],
        keywords=[],
        body=[],
        decorator_list=[],
    )
    cls.body.append(
        ast.Expr(
            value=ast.Constant(
                value=(
                    (
                        f"Async base over {svc}: the surface every "
                        f"subclass guarantees. Hold one when you do not "
                        f"care which implementation answered; construct a "
                        f"subclass to get one."
                    )
                    if proto["abstract"] else
                    (
                        f"Async in-process wrapper over {svc}. The object "
                        f"is constructed lazily on its runner thread."
                        + (f" Inherits {', '.join(proto['inherited'])} from "
                           f"Async{base}." if base and proto.get("inherited") else "")
                    )
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
    if proto["abstract"]:
        # No runner and no target: an abstract base has no implementation
        # to construct. Saying so here beats letting the factory build a
        # trampoline whose overrides do not exist.
        cls.body.append(ast.FunctionDef(
            name="__init__",
            args=ast.arguments(
                posonlyargs=[], args=[ast.arg(arg="self")], vararg=ast.arg(arg="args"),
                kwonlyargs=[], kw_defaults=[], kwarg=ast.arg(arg="kwargs"), defaults=[]),
            body=[ast.Raise(exc=ast.Call(
                func=ast.Name(id="TypeError"),
                args=[ast.Constant(value=(
                    f"Async{svc} is abstract: it is the shared surface, not an "
                    f"implementation. Construct a subclass, or receive one from "
                    f"a call that returns {svc}."))],
                keywords=[]))],
            decorator_list=[], returns=_ann("None", f"{svc}.__init__"),
            type_params=[]))
        for m in proto["methods"]:
            _append_hop_method(cls, proto, m, svc, async_types, bound_policies)
        cls.body.append(_aclose_method())
        mod.body.append(cls)
        ast.fix_missing_locations(mod)
        return mod

    cls.body.append(
        ast.FunctionDef(
            name="__init__",
            args=_ctor_args(proto, async_types),
            body=[
                ast.Assign(
                    targets=[ast.Attribute(value=ast.Name(id="self"), attr="_runner")],
                    value=ast.Call(
                        func=ast.Name(id=runner),
                        args=[
                            # Zero-arg lambda closing over this __init__'s
                            # parameters, so the target is built on the
                            # runner's own thread, not the caller's:
                            #   lambda: DerivedPath(unwrap_arg(drv_path), ...)
                            # Each argument goes through unwrap_arg, so a
                            # wrapper passed in contributes its target
                            # object rather than the async shell.
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
                                        ast.Call(
                                            func=ast.Name(id="unwrap_arg"),
                                            args=[ast.Name(id=p["name"])],
                                            keywords=[],
                                        )
                                        for p in proto["ctor"]
                                    ],
                                    keywords=[],
                                ),
                            )
                        ],
                        keywords=init_kwargs,
                    ),
                )
            ],
            decorator_list=[],
            returns=_ann("None", f"{svc}.__init__"),
            type_params=[],
        )
    )

    for m in proto["methods"]:
        _append_hop_method(cls, proto, m, svc, async_types, bound_policies)

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


FREE_MODULE = "free_functions"


def free_function_module(protos: list[dict], async_types: set[str]) -> ast.Module:
    """Emit module-level coroutines for the bindings' free functions.

    They have no instance, so there is no runner to hop through and no
    handle to hold - just the shared pool, which is what "pool" means
    everywhere else. The sync function is imported under an underscore
    alias so the coroutine can take its plain name.
    """
    mod = ast.Module(body=[], type_ignores=[])
    mod.body.append(ast.Expr(value=ast.Constant(
        value="Generated async wrappers for the bindings' module-level "
              "functions - do not edit. Built via ast at Nix build time.")))

    annotations = []
    for proto in protos:
        annotations += [_param_ann(p["type"], async_types) for p in proto["params"]]
        annotations.append(proto["return_type"])
    used = _annotation_names(annotations)
    mod.body.extend(_sibling_imports(used))
    ann_import = _fake_library_import(used)
    if ann_import is not None:
        mod.body.append(ann_import)
    mod.body.append(ast.ImportFrom(
        module="fake_library",
        names=[ast.alias(name=p["name"], asname="_" + p["name"])
               for p in sorted(protos, key=lambda x: x["name"])],
        level=0))
    mod.body.append(ast.ImportFrom(
        module="_runtime", names=[ast.alias(name="call_function")], level=1))

    for proto in protos:
        body: list[ast.stmt] = []
        if proto["doc"]:
            body.append(ast.Expr(value=ast.Constant(value=proto["doc"])))
        body.append(ast.Return(value=ast.Await(value=ast.Call(
            func=ast.Name(id="call_function"),
            args=[ast.Name(id="_" + proto["name"]),
                  ast.List(elts=[ast.Name(id=p["name"]) for p in proto["params"]])],
            keywords=[]))))
        mod.body.append(ast.AsyncFunctionDef(
            name=proto["name"],
            args=ast.arguments(
                posonlyargs=[],
                args=[ast.arg(arg=p["name"],
                              annotation=_ann(_param_ann(p["type"], async_types),
                                              f"{proto['name']}:{p['name']}"))
                      for p in proto["params"]],
                vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]),
            body=body,
            decorator_list=[],
            returns=_ann(proto["return_type"], proto["name"]),
            type_params=[]))

    ast.fix_missing_locations(mod)
    return mod


def init_module(all_names: list[str], free_names: list[str] | None = None) -> ast.Module:
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
    free_names = free_names or []
    if free_names:
        mod.body.append(ast.ImportFrom(
            module=FREE_MODULE,
            names=[ast.alias(name=n) for n in free_names],
            level=1))
    mod.body.append(
        ast.Assign(
            targets=[ast.Name(id="__all__")],
            value=ast.List(
                elts=[ast.Constant(value=f"Async{n}") for n in all_names]
                + [ast.Constant(value=n) for n in free_names]
            ),
        )
    )
    ast.fix_missing_locations(mod)
    return mod
