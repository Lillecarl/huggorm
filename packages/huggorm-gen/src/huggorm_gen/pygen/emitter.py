"""
Emit ast trees from protocol dicts.

Pure tree building — no I/O, no imports of the spec. Everything the
emitter needs arrives in the protocol dict a declaration produced.
"""

import ast
from typing import Any

from huggorm_gen.payload.wiretypes import dotted_heads, names_in

# One class, method or function as a plain dict. See model.Proto.
Proto = dict[str, Any]

RUNNER_BY_THREADING = {
    "affine": "AffineRunner",
    "pool": "PoolRunner",
}

# Annotation atoms that never need an import. Everything else must be
# imported from huggorm_bindings, or get_type_hints raises NameError -
# invisible on Python 3.14 (PEP 649 lazy annotations), fatal below.
_BUILTIN_TYPES = {"None", "Any", "str", "int", "float", "bool", "bytes",
                  "object", "dict", "list", "tuple", "set"}


def _param_ann(type_str: str, async_types: set[str]) -> str:
    """The annotation a parameter really accepts.

    unwrap_arg takes either side: the sync binding object or the async
    wrapper over it, which contributes its target. Annotating the sync
    type alone was a lie - in-process callers pass wrappers (that is
    the whole surface), the RPC server passes sync objects. Say both."""
    return f"{type_str} | Async{type_str}" if type_str in async_types else type_str


def _arguments(leading: list[ast.arg], params: list[Proto],
               types: list[str], where: str) -> ast.arguments:
    """`leading` plus one argument per declared parameter, annotated
    and defaulted.

    `types` is the annotation each parameter is written with. The
    caller resolves it, because the same declared type is spelled
    differently in an async wrapper, in a protocol and in a stub.

    The default is written from the manifest's source string, so every
    surface offers the same one. A caller that omits the argument gets
    the same value in-process and over RPC, and the wire never has to
    represent absence."""
    args = list(leading)
    defaults: list[ast.expr] = []
    for p, type_str in zip(params, types, strict=True):
        if p["default"] == "None":
            # A parameter that may be omitted is spelled `T | None`.
            # `output: str = None` is implicit Optional, which strict
            # typecheckers reject and which misdescribes the default
            # the emitter itself writes. Here rather than at each call
            # site: the four surfaces spell the TYPE differently and
            # none of them spells this differently.
            type_str = f"{type_str} | None"
        args.append(ast.arg(arg=p["name"],
                            annotation=_ann(type_str, f"{where}:{p['name']}")))
        if p["default"] is not None:
            defaults.append(_ann(p["default"], f"{where}:{p['name']}="))
        elif defaults:
            raise ValueError(
                f"{where}: required parameter {p['name']!r} follows a "
                f"defaulted one")
    return ast.arguments(posonlyargs=[], args=args, vararg=None,
                         kwonlyargs=[], kw_defaults=[], kwarg=None,
                         defaults=defaults)


def _default_names(params: list[Proto]) -> list[str]:
    """The default expressions in one parameter list, as strings.

    Import collection reads these beside the annotations: a default of
    `ContentAddressMethod.NAR` names a type the module has to import,
    and the annotation only happens to name the same one."""
    return [p["default"] for p in params if p["default"] is not None]


def _ctor_args(proto: Proto, async_types: set[str]) -> ast.arguments:
    """Typed __init__ parameters from the declared constructor.

    This used to be `*args, **kwargs` forwarded blind, because nothing
    knew the constructor's shape. It does now (see model.constructor_
    signature), so the wrapper states it: wrong arity fails at the call
    site instead of inside a lazy factory on some worker thread, and a
    typechecker can see it."""
    return _arguments([ast.arg(arg="self")], proto["ctor"],
                      [_param_ann(p["type"], async_types)
                       for p in proto["ctor"]],
                      f"{proto['name']}.__init__")


def _return_ann(rt: str, bound_policies: dict[str, str],
                twins: dict[str, str]) -> str:
    """What the IN-PROCESS wrapper declares it returns.

    Three answers. A proxy is adopted into its Async form. A type with
    a declared async twin is handed back as the twin - same value,
    awaitable methods. Everything else is itself."""
    if rt in bound_policies:
        return f"Async{rt}"
    return twins.get(rt, rt)


def _emitted_annotations(proto: Proto, async_types: set[str],
                         bound_policies: dict[str, str],
                         twins: dict[str, str]) -> list[str]:
    """The annotation strings the emitter will actually write. Import
    collection reads THIS, not the raw protocol types: a return type
    that gets adopted is written as AsyncX and must not drag the sync X
    into the module as an unused import."""
    out = [_param_ann(p["type"], async_types) for p in proto.get("ctor", ())]
    for m in proto["methods"]:
        out += [_param_ann(p["type"], async_types) for p in m["params"]]
        out.append(_return_ann(m["return_type"], bound_policies, twins))
    return out


def _emitted_defaults(proto: Proto) -> list[str]:
    """The default expressions the emitter will write. Kept apart from
    the annotations because only one of the two may be read for module
    heads - see _annotation_names."""
    return [d for m in proto["methods"] for d in _default_names(m["params"])]


def _annotation_names(annotations: list[str]) -> set[str]:
    """Every name an annotation list mentions, module heads excluded.

    Two dotted things reach the emitter and only one is a module. An
    annotation `pathlib.Path` names a type in a module, imported as
    itself. A default `ContentAddressMethod.NAR` names an attribute on
    a CLASS, which has to come from huggorm_bindings like any other -
    which is why the two lists stay apart and only annotations are
    asked for their heads."""
    out: set[str] = set()
    for a in annotations:
        out |= names_in(a)
    return out - _module_heads(annotations)


def _module_heads(annotations: list[str]) -> set[str]:
    """The modules an ANNOTATION list names by a dotted type.

    Pass annotations only. A default's dotted head is a class, and
    calling this on one would import a module that does not exist."""
    return {h for a in annotations for h in dotted_heads(a)}


def _foreign_imports(annotations: list[str]) -> list[ast.Import]:
    """`import pathlib` for every module an emitted module annotates
    with by a dotted name.

    A plain import, not a from-import: the annotation is written
    dotted, `pathlib.Path`, so the module name is what has to be
    bound. Which modules those are is read off the annotations rather
    than listed anywhere."""
    return [ast.Import(names=[ast.alias(name=m)])
            for m in sorted(_module_heads(annotations))]


# The SUM types, by alias name. A union is not in huggorm_bindings and
# cannot be: the alias is Python and the module that would hold it is a
# compiled extension. `_unions.py` is generated from the manifest
# instead, so the one statement of `DerivedPath = StorePath |
# DerivedPathBuilt` is the declaration and everything else derives.
UNIONS_MODULE = "._unions"
_UNION_NAMES: set[str] = set()


def emitter_union_names(names: set[str]) -> None:
    """Which annotation names are ALIASES rather than bound classes.

    Told once, before anything is written. There is no way to tell the
    two apart from a name, and the difference decides which import an
    emitted module gets."""
    _UNION_NAMES.clear()
    _UNION_NAMES.update(names)


def _huggorm_bindings_import(names: set[str]) -> list[ast.ImportFrom]:
    """Import the types an emitted module annotates with.

    Two sources, because a union has no home in the bindings. A CLASS
    comes from huggorm_bindings, which is where it is bound; an ALIAS
    comes from the generated `_unions`, which is where it is written.

    Async* names are excluded: those come from sibling modules. So are
    dotted module heads, which _annotation_names already drops - they
    are imported as themselves."""
    usable = sorted(
        n for n in names
        if n not in _BUILTIN_TYPES and not n.startswith("Async")
    )
    out = []
    bound = [n for n in usable if n not in _UNION_NAMES]
    if bound:
        out.append(ast.ImportFrom(
            module="huggorm_bindings",
            names=[ast.alias(name=n) for n in bound], level=0))
    aliases = [n for n in usable if n in _UNION_NAMES]
    if aliases:
        out.append(ast.ImportFrom(
            module=UNIONS_MODULE.lstrip("."),
            names=[ast.alias(name=n) for n in aliases], level=1))
    return out


# The emitted `_policy.py`'s own docstring. Out here rather than
# inline, because it is prose a reader of the OUTPUT sees and an
# 800-column string literal in an emitter argument list is neither
# readable nor lintable.
POLICY_DOC = """The wire policy of every declared type.

Four tables the codec needs and no caller does: what KIND each type
crosses as, what a wire value is made of, which names are string
vocabularies, and what a sum type's arms are in declared order.

They came out of `manifest.json`, read at run time by a codec a
typechecker could tell nothing about - every one of them was a
`dict[str, Any]` off a JSON load. `check_manifest` existed for
exactly that reason: a manifest from another generator "would answer
wrong, one lookup at a time". An emitted module ships with the code
that reads it, so there is no other generator to defend against.

Arms in DECLARED order, because that is the order the schema numbered
the oneof's fields in and a renumbering is a wire change. Tuples
rather than lists, so nothing downstream reorders one in place."""


def policy_module(manifest: Proto) -> str:
    """`_policy.py`: the wire policy of every declared type.

    Four tables the codec needs and no caller does: what KIND each
    type crosses as, what a wire value is made of, which names are
    string vocabularies, and what a sum type's arms are in declared
    order.

    They came out of `manifest.json`, read at run time by a codec that
    a typechecker could tell nothing about - every one of them was a
    `dict[str, Any]` off a JSON load. `check_manifest` existed for
    exactly that reason: its own comment says a manifest from another
    generator *"would answer wrong, one lookup at a time"*. An emitted
    module ships with the code that reads it, so there is no other
    generator to defend against.

    Arms in DECLARED order, because that is the order the schema
    numbered the oneof's fields in and a renumbering is a wire change.
    A tuple rather than a list, so nothing downstream can reorder them
    in place.
    """
    kinds, fields = [], []
    for group in ("wrappers", "returned_types"):
        for name, proto in manifest[group].items():
            kinds.append((name, ast.Constant(value=proto["wire"])))
            fields.append((name, ast.Tuple(elts=[
                ast.Call(func=ast.Name(id="Arg"),
                         args=[ast.Constant(value=f[0]),
                               ast.Constant(value=f[1])],
                         keywords=[])
                for f in proto["wire_fields"]])))
    body: list[ast.stmt] = [
        ast.Expr(value=ast.Constant(value=POLICY_DOC)),
        ast.ImportFrom(module="._callspec", names=[ast.alias(name="Arg")],
                       level=0),
    ]
    for var, ann, rows in (("WIRE_KIND", "dict[str, str]", kinds),
                           ("WIRE_FIELDS", "dict[str, tuple[Arg, ...]]",
                            fields)):
        body.append(ast.AnnAssign(
            target=ast.Name(id=var), annotation=_ann(ann, var),
            value=ast.Dict(keys=[ast.Constant(value=n) for n, _ in rows],
                           values=[v for _, v in rows]),
            simple=1))
    body.append(ast.AnnAssign(
        target=ast.Name(id="ENUMS"), annotation=_ann("frozenset[str]", "ENUMS"),
        value=ast.Call(func=ast.Name(id="frozenset"),
                       args=[ast.Set(elts=[ast.Constant(value=n)
                                           for n in sorted(manifest["enums"])])]
                       if manifest["enums"] else [],
                       keywords=[]),
        simple=1))
    body.append(ast.AnnAssign(
        target=ast.Name(id="UNION_ARMS"),
        annotation=_ann("dict[str, tuple[str, ...]]", "UNION_ARMS"),
        value=ast.Dict(
            keys=[ast.Constant(value=n) for n in manifest["unions"]],
            values=[ast.Tuple(elts=[ast.Constant(value=a) for a in arms])
                    for arms in manifest["unions"].values()]),
        simple=1))
    return ast.unparse(ast.Module(body=body, type_ignores=[])) + "\n"


def unions_module(unions: dict[str, list[str]]) -> str:
    """`_unions.py`: one alias per declared sum type.

    Nothing but aliases, and every one derived from the manifest - so
    the declaration says `DerivedPath = StorePath | DerivedPathBuilt`
    once and this is the same sentence in the package a caller
    imports."""
    arms = sorted({a for v in unions.values() for a in v})
    body: list[ast.stmt] = [
        ast.Expr(value=ast.Constant(value=(
            "The declared SUM types, as the aliases they are.\n\n"
            "A union has no home in `huggorm_bindings`: the alias is "
            "Python and the module that binds its arms is a compiled "
            "extension. So it is written here, from the same "
            "declaration the arms came from.\n"))),
        ast.ImportFrom(module="huggorm_bindings",
                       names=[ast.alias(name=a) for a in arms], level=0),
    ]
    for alias, members in unions.items():
        value: ast.expr = ast.Name(id=members[0])
        for arm in members[1:]:
            value = ast.BinOp(left=value, op=ast.BitOr(),
                              right=ast.Name(id=arm))
        body.append(ast.Assign(targets=[ast.Name(id=alias)], value=value))
    body.append(ast.Assign(
        targets=[ast.Name(id="__all__")],
        value=ast.List(elts=[ast.Constant(value=a) for a in unions])))
    return ast.unparse(ast.fix_missing_locations(
        ast.Module(body=body, type_ignores=[]))) + "\n"


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


def returned_module(proto: Proto,
                    async_types: set[str] | None = None,
                    bound_policies: dict[str, str] | None = None,
                    twins: dict[str, str] | None = None) -> ast.Module:
    """
    Emit Async<Bound> for a returned value type (e.g. Poop).

    Constructed with (obj, runner): the object was already produced on
    the producer's thread; attach_runner picks the right execution
    strategy from the type's declared policy.

    bound_policies is what a wrapper module gets too: a returned type
    can produce another one (a Value holds Values), and such a return
    has to be adopted rather than handed back as a bare binding object.
    """
    svc = proto["name"]
    policy = proto["threading"]
    policy_wire = proto["wire"]
    async_types = async_types or set()
    bound_policies = bound_policies or {}
    twins = twins or {}

    mod = ast.Module(body=[], type_ignores=[])
    annotations = _emitted_annotations(proto, async_types, bound_policies, twins)
    defaults = _emitted_defaults(proto)
    used = _annotation_names(annotations) | _annotation_names(defaults)
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
    typing_names = {"Any"} if "Any" in used else set()
    typing_names.add("Any")  # the constructor takes the produced object
    if any(m["return_type"] != "None" for m in proto["methods"]):
        typing_names.add("cast")
    mod.body.append(ast.ImportFrom(
        module="typing",
        names=[ast.alias(name=n) for n in sorted(typing_names)], level=0))
    mod.body.append(ast.ImportFrom(
        module="_runtime",
        names=[ast.alias(name="BaseRunner"), ast.alias(name="attach_runner")],
        level=1))
    # A value that produces values names its OWN async class, which is
    # defined right here: importing it would be a self-import.
    mod.body.extend(_sibling_imports(used - {f"Async{svc}"}))
    mod.body.extend(_foreign_imports(annotations))
    mod.body.extend(_huggorm_bindings_import(used))

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
    cls.body.append(_runner_decl())
    cls.body.append(
        ast.FunctionDef(
            name="__init__",
            args=ast.arguments(
                posonlyargs=[],
                args=[ast.arg(arg="self"),
                      ast.arg(arg="obj", annotation=_ann("Any", f"{svc}.__init__")),
                      ast.arg(arg="runner",
                              annotation=_ann("BaseRunner", f"{svc}.__init__"))],
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
            returns=_ann("None", f"{svc}.__init__"),
            type_params=[],
        )
    )

    mod.body.append(cls)
    _append_methods_and_aclose(cls, proto, svc, async_types, bound_policies,
                               twins)
    ast.fix_missing_locations(mod)
    return mod


def _append_methods_and_aclose(cls: ast.ClassDef, proto: Proto, svc: str,
                               async_types: set[str],
                               bound_policies: dict[str, str],
                               twins: dict[str, str]) -> None:
    # Same emission as a wrapper's methods, adoption included. It used
    # to be a second copy of the hop body without the adoption branch,
    # so a returned type producing another one handed back the bare
    # binding object - alive in process, and a type error everywhere
    # else, because every other surface says AsyncX.
    for m in proto["methods"]:
        _append_hop_method(cls, proto, m, svc, async_types, bound_policies,
                           twins)
    cls.body.append(_aclose_method())


def _hop_call(method_name: str, params: list[Proto]) -> ast.Call:
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


def _forward(call: ast.expr, return_type: str) -> ast.stmt:
    """Return the result of an awaited forward, typed.

    The runtime hands back Any - it dispatches by method name onto an
    object it knows nothing about. The declared type is the manifest's
    claim about that method, so the cast is where the claim is made
    rather than a silent Any leaking into every caller. A method
    returning None does not return at all: casting to None is not a
    thing, and there is nothing to hand back."""
    if return_type == "None":
        return ast.Expr(value=ast.Await(value=call))
    return ast.Return(value=ast.Call(
        func=ast.Name(id="cast"),
        args=[_ann(return_type, "cast"), ast.Await(value=call)],
        keywords=[]))


def _hop_return(method_name: str, params: list[Proto],
                return_type: str = "None") -> ast.stmt:
    return _forward(_hop_call(method_name, params), return_type)


def _runner_decl() -> ast.AnnAssign:
    """The attribute every emitted method reaches through.

    Declared, not merely assigned: the abstract base never assigns it -
    its __init__ refuses - so without this its own method bodies read
    an attribute a typechecker cannot see."""
    return ast.AnnAssign(target=ast.Name(id="_runner"),
                         annotation=ast.Name(id="BaseRunner"),
                         value=None, simple=1)


def _append_hop_method(cls: ast.ClassDef, proto: Proto, m: Proto, svc: str,
                       async_types: set[str],
                       bound_policies: dict[str, str],
                       twins: dict[str, str]) -> None:
    """One `async def` that hops to the runner. Shared by the abstract
    base and its subclasses: the body is identical either way, which is
    exactly why a base can carry it - the runner comes from whichever
    __init__ ran."""
    params = _arguments(
        [ast.arg(arg="self")], m["params"],
        [_param_ann(p["type"], async_types) for p in m["params"]],
        f"{svc}.{m['name']}")
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
    elif rt in twins:
        # Same value, other spelling. anyio.Path takes any path-like,
        # so the wrapper constructs one rather than casting: a cast
        # would claim the awaitable methods without adding them.
        body.append(ast.Return(value=ast.Call(
            func=_ann(twins[rt], f"{svc}.{m['name']}"),
            args=[ast.Await(value=_hop_call(m["name"], m["params"]))],
            keywords=[])))
    else:
        body.append(_hop_return(m["name"], m["params"], rt))
    cls.body.append(ast.AsyncFunctionDef(
        name=m["name"],
        args=params,
        body=body,
        decorator_list=[],
        returns=_ann(_return_ann(rt, bound_policies, twins),
                     f"{svc}.{m['name']}"),
        type_params=[]))


def wrapper_module(proto: Proto, bound_policies: dict[str, str] | None = None,
                   twins: dict[str, str] | None = None,
                   async_types: set[str] | None = None) -> ast.Module:
    """Emit Async<Svc>. bound_policies maps returned-type names to their
    declared threading policy; those methods adopt the produced object
    into an attached runner instead of returning it raw. async_types is
    every generated wrapper name, used to widen parameter annotations."""
    svc = proto["name"]
    bound_policies = bound_policies or {}
    async_types = async_types or set()
    twins = twins or {}
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

    annotations = _emitted_annotations(proto, async_types, bound_policies, twins)
    used_types = (_annotation_names(annotations)
                  | _annotation_names(_emitted_defaults(proto))
                  | {"None"})  # None: aclose
    typing_names = {"Any"} if "Any" in used_types else set()
    if proto["abstract"]:
        typing_names.add("Any")  # the refusing __init__ takes *args/**kwargs
    # A forward hands back Any; the declared type is the manifest's
    # claim, and cast is where it gets made. Adopted returns build a
    # real object instead, and None returns do not return.
    if any(m["return_type"] != "None" and m["return_type"] not in bound_policies
           for m in proto["methods"]):
        typing_names.add("cast")
    if typing_names:
        mod.body.append(ast.ImportFrom(
            module="typing",
            names=[ast.alias(name=n) for n in sorted(typing_names)], level=0))
    # Never import our own class from ourselves.
    if proto.get("async_base"):
        used_types.add(f"Async{proto['async_base']}")
    mod.body.extend(_sibling_imports(used_types - {f"Async{svc}"}))
    mod.body.extend(_foreign_imports(annotations))

    # An abstract base constructs nothing, so it imports neither the
    # sync target nor a runner class. It still annotates with the target
    # if a method mentions it, which is why the subtraction below is
    # conditional too.
    constructs = not proto["abstract"]
    runtime_names = [] if proto.get("async_base") else ["BaseRunner"]
    if constructs:
        mod.body.append(ast.ImportFrom(
            module="huggorm_bindings", names=[ast.alias(name=svc)], level=0))
    mod.body.extend(_huggorm_bindings_import(
        used_types - ({svc} if constructs else set())))
    if constructs:
        runtime_names.append(runner)
    if runtime_names:
        mod.body.append(ast.ImportFrom(
            module="_runtime",
            names=[ast.alias(name=n) for n in sorted(runtime_names)], level=1))
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
    if base is None:
        cls.body.append(_runner_decl())

    init_kwargs = []
    if proto["threading"] == "affine":
        init_kwargs.append(ast.keyword(
            arg="name", value=ast.Constant(value=f"huggorm-affine-{svc}")))
    if proto["abstract"]:
        # No runner and no target: an abstract base has no implementation
        # to construct. Saying so here beats letting the factory build a
        # trampoline whose overrides do not exist.
        cls.body.append(ast.FunctionDef(
            name="__init__",
            args=ast.arguments(
                posonlyargs=[], args=[ast.arg(arg="self")],
                vararg=ast.arg(arg="args", annotation=_ann("Any", f"{svc}.__init__")),
                kwonlyargs=[], kw_defaults=[],
                kwarg=ast.arg(arg="kwargs", annotation=_ann("Any", f"{svc}.__init__")),
                defaults=[]),
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
            _append_hop_method(cls, proto, m, svc, async_types, bound_policies,
                           twins)
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
        _append_hop_method(cls, proto, m, svc, async_types, bound_policies,
                           twins)

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


def _future_annotations() -> ast.ImportFrom:
    """Lazy annotations, so a class may name one defined further down.

    The protocol and rpc modules each hold the whole surface, and a
    method on the first class can return the last one. PEP 649 already
    defers evaluation on 3.14, but the emitted package is meant to be
    readable and portable below it."""
    return ast.ImportFrom(module="__future__",
                          names=[ast.alias(name="annotations")], level=0)


def _sync_imports(annotations: list[str], defined_here: set[str],
                  defaults: list[str] | None = None) -> list[ast.ImportFrom]:
    """Import the binding types an all-in-one module annotates with.

    Unlike the per-class wrappers there are no siblings to import from:
    everything else the module names, it defines."""
    used = ((_annotation_names(annotations)
             | _annotation_names(defaults or []))
            - _BUILTIN_TYPES - defined_here)
    return _huggorm_bindings_import(used)


def _params(m: Proto, cls_name: str, ann: dict[str, str]) -> ast.arguments:
    """`self` plus one typed argument per declared parameter. `ann` maps
    a declared type to the annotation this module writes for it."""
    return _arguments(
        [ast.arg(arg="self")], m["params"],
        [ann.get(p["type"], p["type"]) for p in m["params"]],
        f"{cls_name}.{m['name']}")


def protocol_module(manifest: Proto, ordered: list[Proto],
                    adoptable: set[str]) -> ast.Module:
    """Emit one Protocol per wrapped class: the surface a caller can
    program against without knowing whether the object answering is in
    this process or on the far side of a socket.

    Methods with a proxy parameter are absent, and the manifest says
    why for each of them (see surface.protocol_blockers). Everything
    else is here, including every method whose types tasks/025 made
    identical on both sides."""
    from huggorm_gen.pygen.surface import ACLOSE, protocol_name

    defined = {protocol_name(p["name"]) for p in ordered}

    def ret_ann(rt: str) -> str:
        return protocol_name(rt) if rt in adoptable else rt

    annotations: list[str] = []
    defaults: list[str] = []
    for proto in ordered:
        for m in proto["methods"]:
            if m["protocol_blockers"]:
                continue
            annotations += [p["type"] for p in m["params"]]
            defaults += _default_names(m["params"])
            annotations.append(ret_ann(m["return_type"]))

    mod = ast.Module(body=[], type_ignores=[])
    mod.body.append(ast.Expr(value=ast.Constant(value=(
        "Generated protocols: the surface both implementations share - do "
        "not edit. One per wrapped class, mirroring the wrapper hierarchy, "
        "so a function typed against StoreLike accepts an in-process "
        "AsyncStore and a remote RPCStore alike."))))
    mod.body.append(_future_annotations())
    mod.body.append(ast.ImportFrom(
        module="typing",
        names=[ast.alias(name="Protocol"), ast.alias(name="runtime_checkable")],
        level=0))
    mod.body.extend(_foreign_imports(annotations))
    sync = _sync_imports(annotations, defined, defaults)
    mod.body.extend(sync)

    for proto in ordered:
        name = proto["name"]
        base = proto.get("async_base")
        bases: list[ast.expr] = (
            [ast.Name(id=protocol_name(base))] if base else [])
        bases.append(ast.Name(id="Protocol"))
        cls = ast.ClassDef(
            name=protocol_name(name), bases=bases, keywords=[], body=[],
            # isinstance() against this checks that the method NAMES are
            # present and nothing more. The signature gate in the smoke
            # test is what proves an implementation really conforms.
            decorator_list=[ast.Name(id="runtime_checkable")],
            type_params=[])
        offered = [m for m in proto["methods"] if not m["protocol_blockers"]]
        withheld = [m["name"] for m in proto["methods"] if m["protocol_blockers"]]
        cls.body.append(ast.Expr(value=ast.Constant(value=(
            f"What every {name} implementation promises"
            + (f", on top of {protocol_name(base)}." if base else ".")
            + (f" {', '.join(sorted(withheld))} cannot be promised: see "
               f"protocol_blockers in the manifest." if withheld else "")))))
        for m in offered:
            body: list[ast.stmt] = []
            if m["doc"]:
                body.append(ast.Expr(value=ast.Constant(value=m["doc"])))
            body.append(ast.Expr(value=ast.Constant(value=Ellipsis)))
            cls.body.append(ast.AsyncFunctionDef(
                name=m["name"],
                args=_params(m, name, {}),
                body=body,
                decorator_list=[],
                returns=_ann(ret_ann(m["return_type"]), f"{name}.{m['name']}"),
                type_params=[]))
        if base is None:
            cls.body.append(ast.AsyncFunctionDef(
                name=ACLOSE,
                args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="self")],
                                   vararg=None, kwonlyargs=[], kw_defaults=[],
                                   kwarg=None, defaults=[]),
                body=[
                    ast.Expr(value=ast.Constant(value=(
                        "Release this object. In process that shuts the "
                        "runner's thread down; remotely it gives the lease "
                        "back. Either way the object is spent afterwards."))),
                    ast.Expr(value=ast.Constant(value=Ellipsis)),
                ],
                decorator_list=[],
                returns=_ann("None", f"{name}.{ACLOSE}"),
                type_params=[]))
        mod.body.append(cls)

    ast.fix_missing_locations(mod)
    return mod


def _args(params: list[Proto]) -> ast.expr:
    """A declared parameter list, as a tuple of `Arg`."""
    return ast.Tuple(elts=[
        ast.Call(func=ast.Name(id="Arg"),
                 args=[ast.Constant(value=p["name"]),
                       ast.Constant(value=p["type"])],
                 keywords=[])
        for p in params])


def _spec(m: Proto) -> ast.expr:
    """One method's call spec, as a `Call`.

    A typed value, not a dict literal. The dict came straight out of
    the manifest and carried its whole entry; a checker could see
    nothing in it, which made the one part of the generated client a
    caller cannot read also the one part nothing verified.

    The docstring is dropped: it is already on the method. So are the
    parameter defaults - the method signature resolved them before the
    call reached the runtime, so every argument a spec describes is
    present, and carrying a default here would suggest the runtime
    fills one in."""
    rpc = m["rpc"]
    return ast.Call(
        func=ast.Name(id="Call"),
        args=[ast.Constant(value=rpc["path"]),
              ast.Constant(value=rpc["req"]),
              ast.Constant(value=rpc["resp"]),
              _args(m["params"]),
              ast.Constant(value=m["return_type"])],
        keywords=[])


def _directory(manifest: Proto, ordered: list[Proto]) -> list[ast.stmt]:
    """The two tables a caller reaches BY NAME.

    `NixClient.acquire("Store", "auto")` and
    `NixClient.call_function("gc_stats")` take a string, so neither
    can be a generated method - the name is the argument. They read
    the manifest for it, which was the last thing the client resolved
    at run time.

    A table, then, and there is no way around one: the lookup is the
    API. What changes is that it ships as emitted Python whose entries
    a checker reads, instead of as JSON the build hands over.

    Not every class is here. One that crosses as a VALUE has no handle
    to construct into - a caller builds it locally and passes it as an
    argument - which is what `acquire` missing from an entry means."""
    acquires = []
    for proto in ordered:
        acq = proto.get("acquire")
        # WRAPPERS only, and the restriction is not cosmetic. `ordered`
        # holds the returned types too, and one of them - Value - has
        # an acquire path in the schema. Constructing it remotely was
        # never offered, because a returned type is by definition
        # something a call HANDS BACK.
        if acq is None or proto["name"] not in manifest["wrappers"]:
            continue
        ctor = proto["ctor"]
        acquires.append((proto["name"], ast.Call(
            func=ast.Name(id="Acquire"),
            args=[ast.Constant(value=proto["name"]),
                  ast.Constant(value=acq["path"]),
                  ast.Constant(value=acq["req"]),
                  _args(ctor),
                  ast.Constant(value=sum(1 for p in ctor
                                         if p["default"] is None))],
            keywords=[])))
    free = [(name, _spec(fn))
            for name, fn in sorted(manifest["free_functions"].items())
            if "rpc" in fn]
    # ...and the ones the wire cannot carry, with the reason.
    #
    # A separate table rather than absence, because the two answers
    # differ and a caller can act on the difference: a name nobody
    # declared is a typo, and a declared function with no RPC surface
    # is a policy the build decided and printed. Folding them together
    # told a caller their spelling was wrong when it was not.
    blocked = [(name, ast.Constant(value="; ".join(fn["wire_blockers"])))
               for name, fn in sorted(manifest["free_functions"].items())
               if "rpc" not in fn]
    out: list[ast.stmt] = []
    for var, kind, rows in (("ACQUIRE", "Acquire", acquires),
                            ("FREE", "Call", free),
                            ("NO_RPC", "str", blocked)):
        out.append(ast.AnnAssign(
            target=ast.Name(id=var),
            annotation=_ann(f"dict[str, {kind}]", var),
            value=ast.Dict(keys=[ast.Constant(value=n) for n, _ in rows],
                           values=[v for _, v in rows]),
            simple=1))
    return out


def _spec_name(cls: str, method: str) -> str:
    """What one method's spec constant is called.

    Module level, not a class attribute. A class attribute had to be
    RESTATED by any subclass that adds a method - an attribute shadows
    rather than merges, and an inherited body reads `self._rpc` - so
    the base's whole table was copied into the subclass. A constant per
    method has no such rule.

    Leading underscore, because it is not surface: a caller reads the
    method, not what the build decided the method does."""
    return f"_{cls}_{method}"


def rpc_module(manifest: Proto, ordered: list[Proto],
               wrapped: set[str]) -> ast.Module:
    """Emit one RPC client class per wrapped class.

    These replace a __getattr__ proxy. That proxy resolved a method
    name against the manifest at call time, which meant a typechecker
    saw nothing at all: it could neither reject a call that does not
    exist nor check the arguments of one that does. A generated class
    has real methods with real signatures, so both directions are
    checked - and the conformance gate compares them against the
    protocol and the in-process wrapper.

    Unlike the in-process side, NO class here is abstract. Locally an
    abstract base has no implementation to construct; remotely every
    handle addresses a real object on the server, and the base is a
    perfectly good view of it - which is the common case, since a
    caller usually does not care which store answered."""
    from huggorm_gen.pygen.surface import ACLOSE, REGISTRY, rpc_class_name

    defined = {rpc_class_name(p["name"]) for p in ordered}

    # A proxy is an RPC class on BOTH sides here: a remote caller holds
    # a handle, never a local object. That is the divergence keeping a
    # method with a proxy parameter off the protocol - the in-process
    # wrapper needs the local object instead.
    ann = {n: rpc_class_name(n) for n in wrapped}

    annotations = ["str"]
    defaults: list[str] = []
    for proto in ordered:
        # The methods this module will WRITE, so a type named only by a
        # method with no rpc does not become an unused import.
        for m in (m for m in proto["methods"] if "rpc" in m):
            annotations += [ann.get(p["type"], p["type"]) for p in m["params"]]
            defaults += _default_names(m["params"])
            annotations.append(ann.get(m["return_type"], m["return_type"]))

    mod = ast.Module(body=[], type_ignores=[])
    mod.body.append(ast.Expr(value=ast.Constant(value=(
        "Generated RPC clients - do not edit. One per wrapped class, "
        "mirroring the wrapper hierarchy. Each method carries the call "
        "spec the manifest gave it, so a call needs no lookup and names "
        "nothing the build did not put there."))))
    mod.body.append(_future_annotations())
    mod.body.append(ast.ImportFrom(
        module="typing",
        names=[ast.alias(name="Any"), ast.alias(name="Protocol"),
               ast.alias(name="cast")], level=0))
    # The call spec's own types. Copied into this package rather than
    # imported from the generator, which does not ship.
    mod.body.append(ast.ImportFrom(
        module="._callspec",
        names=[ast.alias(name="Acquire"), ast.alias(name="Arg"),
               ast.alias(name="Call")], level=0))
    mod.body.extend(_foreign_imports(annotations))
    sync = _sync_imports(annotations, defined | {CLIENT_PROTOCOL},
                         defaults)
    mod.body.extend(sync)

    # What these classes need from whatever is driving them. Declaring
    # it as a Protocol keeps the dependency pointing the right way: the
    # generated package describes what it requires, and the hand-written
    # client satisfies it without either importing the other.
    client_p = ast.ClassDef(
        name=CLIENT_PROTOCOL, bases=[ast.Name(id="Protocol")], keywords=[],
        body=[ast.Expr(value=ast.Constant(value=(
            "What an RPC class needs from its client. The client owns "
            "the connection, the codec and the handle lifetime; these "
            "classes own the surface.")))],
        decorator_list=[], type_params=[])
    for name, args, ret in (
        # handle_id is Optional because release() blanks it. Passing a
        # blanked one is a real mistake, and the client answers it with
        # a message instead of a protobuf failure.
        ("invoke", [("spec", "Call"), ("handle_id", "str | None"),
                    ("args", "list[Any]")], "Any"),
        ("release", [("obj", "Any")], "None"),
    ):
        client_p.body.append(ast.AsyncFunctionDef(
            name=name,
            args=ast.arguments(
                posonlyargs=[],
                args=[ast.arg(arg="self")]
                + [ast.arg(arg=a, annotation=_ann(t, f"{CLIENT_PROTOCOL}.{name}"))
                   for a, t in args],
                vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None,
                defaults=[]),
            body=[ast.Expr(value=ast.Constant(value=Ellipsis))],
            decorator_list=[],
            returns=_ann(ret, f"{CLIENT_PROTOCOL}.{name}"), type_params=[]))
    mod.body.append(client_p)
    mod.body.extend(_directory(manifest, ordered))

    for proto in ordered:
        name = proto["name"]
        base = proto.get("async_base")
        # This class's call specs, at MODULE level and before it.
        #
        # They were `_rpc`, one dict per class. A subclass that added a
        # method had to restate its base's whole table, because an
        # attribute shadows rather than merges and an inherited body
        # read `self._rpc`. A constant per method has no such rule, and
        # a checker reads every field of one.
        for m in (m for m in proto["methods"] if "rpc" in m):
            mod.body.append(ast.Assign(
                targets=[ast.Name(id=_spec_name(name, m["name"]))],
                value=_spec(m)))
        cls = ast.ClassDef(
            name=rpc_class_name(name),
            bases=[ast.Name(id=rpc_class_name(base))] if base else [],
            keywords=[], body=[], decorator_list=[], type_params=[])
        cls.body.append(ast.Expr(value=ast.Constant(value=(
            f"A {name} living behind a handle on a server. Same surface as "
            f"Async{name}, different location."
            + (f" Inherits {', '.join(proto['inherited'])} from "
               f"{rpc_class_name(base)}." if base and proto.get("inherited")
               else "")))))
        cls.body.append(ast.Assign(targets=[ast.Name(id="_wire")],
                                   value=ast.Constant(value=proto["wire"])))
        if base is None:
            for attr, kind in (("_client", CLIENT_PROTOCOL),
                               ("handle_id", "str | None")):
                cls.body.append(ast.AnnAssign(
                    target=ast.Name(id=attr),
                    annotation=_ann(kind, f"{name}.{attr}"),
                    value=None, simple=1))

        # Only the methods that HAVE an rpc. A method the wire cannot
        # carry keeps its in-process wrapper and is simply absent here;
        # the manifest says why, and the protocol drops it too.
        callable_ = [m for m in proto["methods"] if "rpc" in m]

        if base is None:
            cls.body.append(ast.FunctionDef(
                name="__init__",
                args=ast.arguments(
                    posonlyargs=[],
                    args=[ast.arg(arg="self"),
                          ast.arg(arg="client",
                                  annotation=_ann(CLIENT_PROTOCOL,
                                                  f"{name}.__init__")),
                          ast.arg(arg="handle_id",
                                  annotation=_ann("str", f"{name}.__init__"))],
                    vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None,
                    defaults=[]),
                body=[
                    ast.Assign(
                        targets=[ast.Attribute(value=ast.Name(id="self"),
                                               attr="_client")],
                        value=ast.Name(id="client")),
                    ast.Assign(
                        targets=[ast.Attribute(value=ast.Name(id="self"),
                                               attr="handle_id")],
                        value=ast.Name(id="handle_id")),
                ],
                decorator_list=[], returns=_ann("None", f"{name}.__init__"),
                type_params=[]))

        for m in callable_:
            body: list[ast.stmt] = []
            if m["doc"]:
                body.append(ast.Expr(value=ast.Constant(value=m["doc"])))
            body.append(_forward(ast.Call(
                func=ast.Attribute(
                    value=ast.Attribute(value=ast.Name(id="self"),
                                        attr="_client"),
                    attr="invoke"),
                args=[
                    ast.Name(id=_spec_name(name, m["name"])),
                    ast.Attribute(value=ast.Name(id="self"), attr="handle_id"),
                    ast.List(elts=[ast.Name(id=p["name"]) for p in m["params"]]),
                ],
                keywords=[]), ann.get(m["return_type"], m["return_type"])))
            cls.body.append(ast.AsyncFunctionDef(
                name=m["name"],
                args=_params(m, name, ann),
                body=body,
                decorator_list=[],
                returns=_ann(ann.get(m["return_type"], m["return_type"]),
                             f"{name}.{m['name']}"),
                type_params=[]))

        if base is None:
            cls.body.append(ast.AsyncFunctionDef(
                name=ACLOSE,
                args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="self")],
                                   vararg=None, kwonlyargs=[], kw_defaults=[],
                                   kwarg=None, defaults=[]),
                body=[
                    ast.Expr(value=ast.Constant(value=(
                        "Give the lease back. The in-process wrapper shuts "
                        "its runner down here; there is no thread to shut "
                        "down on this side, so the server's copy is what "
                        "gets released."))),
                    ast.Expr(value=ast.Await(value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Attribute(value=ast.Name(id="self"),
                                                attr="_client"),
                            attr="release"),
                        args=[ast.Name(id="self")], keywords=[]))),
                ],
                decorator_list=[], returns=_ann("None", f"{name}.{ACLOSE}"),
                type_params=[]))

        mod.body.append(cls)

    # Annotated: the inferred value type is the join of every class in
    # it, which collapses to type[object] - and object takes no
    # constructor arguments, so a caller could not build one.
    mod.body.append(ast.AnnAssign(
        target=ast.Name(id=REGISTRY),
        annotation=_ann("dict[str, type[Any]]", REGISTRY),
        value=ast.Dict(
            keys=[ast.Constant(value=p["name"]) for p in ordered],
            values=[ast.Name(id=rpc_class_name(p["name"])) for p in ordered]),
        simple=1))
    ast.fix_missing_locations(mod)
    return mod


FREE_MODULE = "free_functions"


def free_function_module(protos: list[Proto],
                        async_types: set[str]) -> ast.Module:
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

    annotations: list[str] = []
    defaults: list[str] = []
    for proto in protos:
        annotations += [_param_ann(p["type"], async_types) for p in proto["params"]]
        defaults += _default_names(proto["params"])
        annotations.append(proto["return_type"])
    used = _annotation_names(annotations) | _annotation_names(defaults)
    if any(p["return_type"] != "None" for p in protos):
        mod.body.append(ast.ImportFrom(
            module="typing", names=[ast.alias(name="cast")], level=0))
    mod.body.extend(_sibling_imports(used))
    mod.body.extend(_foreign_imports(annotations))
    mod.body.extend(_huggorm_bindings_import(used))
    mod.body.append(ast.ImportFrom(
        module="huggorm_bindings",
        names=[ast.alias(name=p["name"], asname="_" + p["name"])
               for p in sorted(protos, key=lambda x: x["name"])],
        level=0))
    mod.body.append(ast.ImportFrom(
        module="_runtime", names=[ast.alias(name="call_function")], level=1))

    for proto in protos:
        body: list[ast.stmt] = []
        if proto["doc"]:
            body.append(ast.Expr(value=ast.Constant(value=proto["doc"])))
        body.append(_forward(ast.Call(
            func=ast.Name(id="call_function"),
            args=[ast.Name(id="_" + proto["name"]),
                  ast.List(elts=[ast.Name(id=p["name"]) for p in proto["params"]])],
            keywords=[]), proto["return_type"]))
        mod.body.append(ast.AsyncFunctionDef(
            name=proto["name"],
            args=_arguments(
                [], proto["params"],
                [_param_ann(p["type"], async_types) for p in proto["params"]],
                proto["name"]),
            body=body,
            decorator_list=[],
            returns=_ann(proto["return_type"], proto["name"]),
            type_params=[]))

    ast.fix_missing_locations(mod)
    return mod


STUB_PACKAGE = "huggorm_bindings-stubs"

# What the generated RPC classes require of whatever drives them.
CLIENT_PROTOCOL = "RPCClient"


def _stub_body(doc: str) -> list[ast.stmt]:
    """A docstring (when there is one) followed by `...`."""
    out: list[ast.stmt] = []
    if doc:
        out.append(ast.Expr(value=ast.Constant(value=doc)))
    out.append(ast.Expr(value=ast.Constant(value=Ellipsis)))
    return out


# What each value dunder looks like from outside. The comparisons take
# `object` because Python's do: `a == 7` is a legal question with the
# answer False, and typing it as the class would make asking it an
# error.
_DUNDER_SIGS = {
    "__eq__": ("object", "bool"), "__ne__": ("object", "bool"),
    "__lt__": ("object", "bool"), "__le__": ("object", "bool"),
    "__gt__": ("object", "bool"), "__ge__": ("object", "bool"),
    "__hash__": (None, "int"), "__repr__": (None, "str"),
    "__str__": (None, "str"),
}


def _stub_dunders(proto: Proto) -> list[ast.stmt]:
    """The value dunders a class defines, as stub declarations.

    Everything else in a stub comes from the protocol dicts, and those
    skip every `_`-prefixed name - which is right for the manifest and
    wrong here. Without these, a typechecker reads object's __eq__ and
    calls `a < b` an error on a class that supports it, and
    `sorted(paths)` an error on a list of them (tasks/046).

    Which ones exist is reflected, not assumed: ordering and __str__
    are per-class, because a store path has a natural order and a
    natural string while a PathInfo has neither."""
    out: list[ast.stmt] = []
    for dunder in proto.get("dunders", ()):
        param, ret = _DUNDER_SIGS[dunder]
        args = [ast.arg(arg="self")]
        if param is not None:
            args.append(ast.arg(
                arg="other",
                annotation=_ann(param, f"{proto['name']}.{dunder}")))
        out.append(ast.FunctionDef(
            name=dunder,
            args=ast.arguments(posonlyargs=[], args=args, vararg=None,
                               kwonlyargs=[], kw_defaults=[], kwarg=None,
                               defaults=[]),
            body=_stub_body(""), decorator_list=[],
            returns=_ann(ret, f"{proto['name']}.{dunder}"), type_params=[]))
    return out


def stub_module(module: str, protos: list[Proto], free_protos: list[Proto],
                produced: set[str], foreign: dict[str, str]) -> ast.Module:
    """Emit the .pyi describing ONE binding module.

    The bindings ship as compiled extensions. A typechecker cannot read
    a .so, so without this every binding type is Any - which is why a
    protocol-typed consumer could catch a call to a method that does
    not exist and NOT catch a str passed where a StorePath is declared
    (tasks/027).

    Everything here already exists in the protocol dicts: every
    method signature and every constructor signature, as the
    declaration wrote them.

    `produced` names the classes that are handed back rather than
    constructed. Their __init__ raises unconditionally, so the stub
    says NoReturn - true, and it makes StorePath() an error at the call
    site instead of a TypeError at runtime.

    These protos are the UNFILTERED ones. The generated surface is not
    the binding surface: the pool policy drops query_derivation from
    Store, and 018 moves the shared methods off the subclasses. Both
    are rules about the async wrappers. A stub describing the sync
    bindings that way would hide LocalStore.query_derivation, which
    exists and which examples/custom.py calls."""
    mod = ast.Module(body=[], type_ignores=[])
    short = module.rsplit(".", 1)[-1]
    mod.body.append(ast.Expr(value=ast.Constant(value=(
        f"Generated type stubs for {module} - do not edit.\n\n"
        f"The module itself is a compiled extension, which carries no "
        f"signatures a typechecker can read. Built via ast at Nix build "
        f"time, from the same protocol dicts as every other surface."))))

    if any(p["name"] in produced for p in protos):
        mod.body.append(ast.ImportFrom(
            module="typing", names=[ast.alias(name="NoReturn")], level=0))
    # A stub describes the SYNC surface unfiltered, so it names every
    # type the bindings do - a foreign module included, whether or not
    # the method that returns one has an rpc.
    written = [p["type"] for pr in protos for m in pr["methods"]
               for p in m["params"]]
    written += [m["return_type"] for pr in protos for m in pr["methods"]]
    written += [p["type"] for pr in protos for p in pr["ctor"]]
    written += [p["type"] for pr in free_protos for p in pr["params"]]
    written += [pr["return_type"] for pr in free_protos]
    mod.body.extend(_foreign_imports(written))
    for name, other in sorted(foreign.items()):
        mod.body.append(ast.ImportFrom(
            module=other.rsplit(".", 1)[-1],
            names=[ast.alias(name=name)], level=1))

    by_name = {p["name"]: p for p in protos}

    def inherited(proto: Proto) -> Proto:
        """Methods a base already declares identically. A subclass
        entry restates everything it inherits, because the declaration
        that built it inherits its base's methods; Python does not, and
        neither should the stub."""
        out: Proto = {}
        for b in proto["bases"]:
            base = by_name.get(b.rsplit(".", 1)[-1])
            if base is None:
                continue
            out |= inherited(base)
            out |= {m["name"]: m for m in base["methods"]}
        return out

    for proto in protos:
        name = proto["name"]
        bases: list[ast.expr] = [
            ast.Name(id=b.rsplit(".", 1)[-1]) for b in proto["bases"]]
        from_base = inherited(proto)
        cls = ast.ClassDef(name=name, bases=bases, keywords=[], body=[],
                           decorator_list=[], type_params=[])
        cls.body.append(ast.Expr(value=ast.Constant(value=(
            proto.get("doc")
            or f"Binding for the C++ {proto['binds']}. Threading "
               f"'{proto['threading']}', wire '{proto['wire']}'."))))
        # The declarations the codegen itself reads. They are real class
        # attributes, so a stub that omitted them would make every
        # reader of them an error.
        for attr, kind in (("_threading", "str"), ("_wire", "str"),
                           ("_binds", "str")):
            cls.body.append(ast.AnnAssign(
                target=ast.Name(id=attr),
                annotation=_ann(kind, f"{name}.{attr}"),
                value=None, simple=1))
        if name in produced:
            cls.body.append(ast.FunctionDef(
                name="__init__",
                args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="self")],
                                   vararg=None, kwonlyargs=[], kw_defaults=[],
                                   kwarg=None, defaults=[]),
                body=_stub_body(
                    f"Always raises: a {name} is produced by another "
                    f"object, never constructed."),
                decorator_list=[],
                returns=_ann("NoReturn", f"{name}.__init__"),
                type_params=[]))
        else:
            cls.body.append(ast.FunctionDef(
                name="__init__", args=_ctor_args(proto, set()),
                body=_stub_body(""), decorator_list=[],
                returns=_ann("None", f"{name}.__init__"), type_params=[]))
        cls.body.extend(_stub_dunders(proto))
        for m in proto["methods"]:
            same = from_base.get(m["name"])
            if same is not None and (
                    [p["type"] for p in same["params"]]
                    == [p["type"] for p in m["params"]]
                    and same["return_type"] == m["return_type"]):
                continue  # inherited unchanged; the base declares it
            cls.body.append(ast.FunctionDef(
                name=m["name"], args=_params(m, name, {}),
                body=_stub_body(m["doc"]), decorator_list=[],
                returns=_ann(m["return_type"], f"{name}.{m['name']}"),
                type_params=[]))
        if len(cls.body) == 1:
            # Docstring only: a class body needs a statement.
            cls.body.append(ast.Expr(value=ast.Constant(value=Ellipsis)))
        mod.body.append(cls)

    for proto in free_protos:
        mod.body.append(ast.FunctionDef(
            name=proto["name"],
            args=_arguments([], proto["params"],
                            [p["type"] for p in proto["params"]],
                            f"{short}.{proto['name']}"),
            body=_stub_body(proto["doc"]),
            decorator_list=[],
            returns=_ann(proto["return_type"], f"{short}.{proto['name']}"),
            type_params=[]))

    ast.fix_missing_locations(mod)
    return mod


def stub_init_module(by_module: dict[str, list[str]]) -> ast.Module:
    """The stub package's __init__.pyi: re-export exactly what the real
    __init__ exports, in the same order."""
    mod = ast.Module(body=[], type_ignores=[])
    mod.body.append(ast.Expr(value=ast.Constant(value=(
        "Generated type stubs for huggorm_bindings - do not edit."))))
    names = []
    for module, exported in by_module.items():
        # `X as X` is what marks a name re-exported from a stub; a plain
        # import is private to the stub and invisible to consumers.
        mod.body.append(ast.ImportFrom(
            module=module.rsplit(".", 1)[-1],
            names=[ast.alias(name=n, asname=n) for n in exported], level=1))
        names += exported
    mod.body.append(ast.Assign(
        targets=[ast.Name(id="__all__")],
        value=ast.List(elts=[ast.Constant(value=n) for n in names])))
    ast.fix_missing_locations(mod)
    return mod


def init_module(all_names: list[str], free_names: list[str] | None = None) -> ast.Module:
    """The package front door: every wrapped class in its three forms -
    the protocol it promises, the in-process implementation and the RPC
    implementation - plus the free functions."""
    from huggorm_gen.pygen.surface import (
        PROTOCOL_MODULE,
        REGISTRY,
        RPC_MODULE,
        async_class_name,
        protocol_name,
        rpc_class_name,
    )

    mod = ast.Module(body=[], type_ignores=[])
    mod.body.append(
        ast.Expr(
            value=ast.Constant(
                value="Generated surface - do not edit. Built via ast at Nix build time."
            )
        )
    )
    for name in all_names:
        fname = f"async_{name.lower()}"
        mod.body.append(ast.ImportFrom(
            module=fname,
            names=[ast.alias(name=async_class_name(name))], level=1))
    mod.body.append(ast.ImportFrom(
        module=PROTOCOL_MODULE,
        names=[ast.alias(name=protocol_name(n)) for n in all_names], level=1))
    mod.body.append(ast.ImportFrom(
        module=RPC_MODULE,
        names=[ast.alias(name=REGISTRY)]
        + [ast.alias(name=rpc_class_name(n)) for n in all_names], level=1))
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
                elts=[ast.Constant(value=f(n))
                      for n in all_names
                      for f in (async_class_name, protocol_name, rpc_class_name)]
                + [ast.Constant(value=REGISTRY)]
                + [ast.Constant(value=n) for n in free_names]
            ),
        )
    )
    ast.fix_missing_locations(mod)
    return mod
