"""
Emit ast trees from protocol dicts.

Pure tree building — no I/O, no imports of the spec. Everything the
emitter needs arrives in the protocol dict a declaration produced.
"""

import ast
from collections.abc import Callable, Sequence
from typing import Any

from huggorm_gen import ir
from huggorm_gen.payload.wiretypes import (
    SCALAR_NAMES,
    dotted_heads,
    names_in,
    python_spelling,
)
from huggorm_gen.pygen.spell import Spelling

# One class, method or function as a plain dict. See model.Proto.
Proto = dict[str, Any]

ASYNC = ir.ASYNC

RUNNER_BY_THREADING = {
    "affine": "AffineRunner",
    "pool": "PoolRunner",
}

# Annotation atoms that never need an import. Everything else must be
# imported from huggorm_bindings, or get_type_hints raises NameError -
# invisible on Python 3.14 (PEP 649 lazy annotations), fatal below.
_BUILTIN_TYPES = {"None", "Any", "str", "int", "float", "bool", "bytes",
                  "object", "dict", "list", "tuple", "set"}






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
        annotation = _ann(type_str, f"{where}:{p['name']}")
        if p["default"] == "None" and not _admits_none(annotation):
            # A parameter that may be omitted is spelled `T | None`.
            # `output: str = None` is implicit Optional, which strict
            # typecheckers reject and which misdescribes the default
            # the emitter itself writes. Here rather than at each call
            # site: the four surfaces spell the TYPE differently and
            # none of them spells this differently.
            annotation = ast.BinOp(left=annotation, op=ast.BitOr(),
                                   right=ast.Constant(value=None))
        args.append(ast.arg(arg=p["name"], annotation=annotation))
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
_UNION_ARMS: dict[str, list[str]] = {}


def emitter_union_names(unions: dict[str, list[str]]) -> None:
    """Which annotation names are ALIASES rather than bound classes,
    and the arms of each.

    Told once, before anything is written. There is no way to tell the
    two apart from a name, and the difference decides which import an
    emitted module gets."""
    _UNION_NAMES.clear()
    _UNION_NAMES.update(unions)
    _UNION_ARMS.clear()
    _UNION_ARMS.update(unions)


# The protocols, by name. A proxy parameter is annotated with one on
# every surface, and the generated `protocols` module defines them.
PROTOCOLS_MODULE = ".protocols"
_PROTOCOL_NAMES: set[str] = set()


def emitter_protocol_names(names: set[str]) -> None:
    """Which annotation names are protocols. Told once, as the unions
    are, before anything is written."""
    _PROTOCOL_NAMES.clear()
    _PROTOCOL_NAMES.update(names)


def _admits_none(annotation: ast.expr) -> bool:
    """Whether a union arm of `annotation` is already None."""
    if isinstance(annotation, ast.Constant):
        return annotation.value is None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _admits_none(annotation.left) or _admits_none(annotation.right)
    return False


def _written_out(name: str) -> ast.expr:
    """`name`, or the arms of the union it names, each written out too."""
    arms = _UNION_ARMS.get(name)
    if arms is None:
        return ast.Name(id=python_spelling(name))
    expanded = _written_out(arms[0])
    for arm in arms[1:]:
        expanded = ast.BinOp(left=expanded, op=ast.BitOr(),
                             right=_written_out(arm))
    return expanded


class _ExpandUnions(ast.NodeTransformer):
    def visit_Name(self, node: ast.Name) -> ast.expr:
        return _written_out(node.id)


def expand_unions(type_str: str) -> str:
    """`type_str` with every union alias written out as its arms.

    For the binding stubs. The alias is Python in the generated
    `_unions` module, and a compiled binding module holds no such
    name, so a stub that NAMED it would name nothing: a typechecker
    reads the type as unknown. Written out, it is the arms, which the
    binding modules do hold."""
    tree = _ExpandUnions().visit(_ann(type_str, "a stub type"))
    return ast.unparse(tree)


def stub_proto(proto: Proto) -> Proto:
    """A copy of a class or function proto with its unions written out."""
    out = dict(proto)
    if "methods" in out:
        out["methods"] = [
            {**m, "return_type": expand_unions(m["return_type"]),
             "params": [{**p, "type": expand_unions(p["type"])}
                        for p in m["params"]]}
            for m in out["methods"]]
    if "ctor" in out:
        out["ctor"] = [{**p, "type": expand_unions(p["type"])}
                       for p in out["ctor"]]
    if "params" in out:
        out["params"] = [{**p, "type": expand_unions(p["type"])}
                         for p in out["params"]]
    if "return_type" in out:
        out["return_type"] = expand_unions(out["return_type"])
    return out


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
    bound = [n for n in usable
             if n not in _UNION_NAMES and n not in _PROTOCOL_NAMES]
    if bound:
        out.append(ast.ImportFrom(
            module="huggorm_bindings",
            names=[ast.alias(name=n) for n in bound], level=0))
    for module, group in ((UNIONS_MODULE, _UNION_NAMES),
                          (PROTOCOLS_MODULE, _PROTOCOL_NAMES)):
        local = [n for n in usable if n in group]
        if local:
            out.append(ast.ImportFrom(
                module=module.lstrip("."),
                names=[ast.alias(name=n) for n in local], level=1))
    return out


# The emitted `_policy.py`'s own docstring. Out here rather than
# inline, because it is prose a reader of the OUTPUT sees and an
# 800-column string literal in an emitter argument list is neither
# readable nor lintable.
POLICY_DOC = """The wire policy of every declared type.

Four tables the codec needs and no caller does: what KIND each type
crosses as, what a wire value is made of, which names are string
vocabularies, and what a sum type's arms are in declared order. The
exception hierarchy is here too, for the same reason and read by the
same kind of codec.

They came out of `manifest.json`, read at run time by a codec a
typechecker could tell nothing about - every one of them was a
`dict[str, Any]` off a JSON load. `check_manifest` existed for
exactly that reason: a manifest from another generator "would answer
wrong, one lookup at a time". An emitted module ships with the code
that reads it, so there is no other generator to defend against.

Arms in DECLARED order, because that is the order the schema numbered
the oneof's fields in and a renumbering is a wire change. Tuples
rather than lists, so nothing downstream reorders one in place."""


def _table(var: str, ann: str,
           rows: Sequence[tuple[str, ast.expr]]) -> ast.stmt:
    """One emitted lookup table, named and annotated.

    Five of them are written this way, and each was three lines of the
    same ast.Dict construction. The annotation is the point of the
    helper as much as the brevity: an inline loop over tables whose
    values are different expression kinds infers the first branch it
    sees and then rejects the second."""
    return ast.AnnAssign(
        target=ast.Name(id=var), annotation=_ann(ann, var),
        value=ast.Dict(keys=[ast.Constant(value=n) for n, _ in rows],
                       values=[v for _, v in rows]),
        simple=1)


def _walk(how: Proto) -> ast.expr:
    """One container's accessors, as a `Walk`.

    A list has `item` and an attribute set has `name` and `value`.
    Spelled the same way here - `value` is what reads the child in
    both - so the walker has one shape rather than two."""
    return ast.Call(func=ast.Name(id="Walk"),
                    args=[ast.Constant(value=how["size"]),
                          ast.Constant(value=how.get("value")
                                       or how["item"]),
                          ast.Constant(value=how.get("name", ""))],
                    keywords=[])


def policy_module(manifest: Proto, ordered: list[Proto]) -> str:
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
        ast.ImportFrom(module="._callspec",
                       names=[ast.alias(name="Acquire"), ast.alias(name="Arg"),
                              ast.alias(name="Call"), ast.alias(name="Tree"),
                              ast.alias(name="Walk")], level=0),
    ]
    # The protobuf package every message and service sits in.
    # grpc_schema decides it, so a rename reaches every consumer.
    body.append(ast.AnnAssign(
        target=ast.Name(id="PKG"), annotation=_ann("str", "PKG"),
        value=ast.Constant(value=manifest["package"]), simple=1))
    body.append(_table("WIRE_KIND", "dict[str, str]", kinds))
    body.append(_table("WIRE_FIELDS", "dict[str, tuple[Arg, ...]]", fields))
    body.append(ast.AnnAssign(
        target=ast.Name(id="ENUMS"), annotation=_ann("frozenset[str]", "ENUMS"),
        value=ast.Call(func=ast.Name(id="frozenset"),
                       args=[ast.Set(elts=[ast.Constant(value=n)
                                           for n in sorted(manifest["enums"])])]
                       if manifest["enums"] else [],
                       keywords=[]),
        simple=1))
    # The exception surface. ERROR_MODULE is where the emitted module
    # lands, which the fault codec imports to construct one; the
    # fields are what it is rebuilt FROM.
    body.append(ast.AnnAssign(
        target=ast.Name(id="ERROR_MODULE"), annotation=_ann("str", "ERROR_MODULE"),
        value=ast.Constant(value=manifest["errors"]["module"] or ""),
        simple=1))
    errs = manifest["errors"]["classes"]
    body.append(_table("ERROR_FIELDS", "dict[str, tuple[Arg, ...]]", [
        (n, ast.Tuple(elts=[
            ast.Call(func=ast.Name(id="Arg"),
                     args=[ast.Constant(value=f[0]),
                           ast.Constant(value=f[1])], keywords=[])
            for f in e["wire_fields"]]))
        for n, e in errs.items()]))
    body.append(_table("UNION_ARMS", "dict[str, tuple[str, ...]]", [
        (n, ast.Tuple(elts=[ast.Constant(value=a) for a in arms]))
        for n, arms in manifest["unions"].items()]))
    # Every method's call spec, ONCE. The client reads these through
    # `rpc.py` and the server reads them through METHODS below, so the
    # two ends of a call cannot disagree about its shape: there is one
    # statement of it and both import that.
    #
    # They lived in `rpc.py`, which made them the client's. The server
    # then built the same specs a second time out of the manifest, and
    # two derivations of one fact is the thing this repo exists to
    # stop.
    methods = []
    for proto in ordered:
        names = []
        for m in (m for m in proto["methods"] if "rpc" in m):
            var = _spec_name(proto["name"], m["name"])
            body.append(ast.Assign(targets=[ast.Name(id=var)], value=_spec(m)))
            names.append(var)
        methods.append((proto["name"], ast.Tuple(
            elts=[ast.Name(id=n) for n in names])))
    body.append(_table("METHODS", "dict[str, tuple[Call, ...]]", methods))
    # The value TREES, and the async class each proxy is adopted into.
    # Both were dug out of the manifest by the server's constructor.
    # Annotated, because the two lists hold different expression
    # types and an inferred one takes the first branch it sees.
    trees: list[tuple[str, ast.expr]] = []
    async_of: list[tuple[str, ast.expr]] = []

    for group in ("wrappers", "returned_types"):
        for name, proto in manifest[group].items():
            # SERVED, not merely wrapped. `ir.ClassModel.entry` stamps an
            # `async_class` NAME on every proxy, and an unserved one
            # gets no such class emitted - so reading the key alone
            # put 'LogStream': 'AsyncLogStream' in this table with
            # nothing behind it, and `server.adopt` would have raised
            # AttributeError on the first handle (huggorm#32).
            if proto["wire"] == "proxy" and "async_class" in proto:
                async_of.append((name,
                                 ast.Constant(value=proto["async_class"])))
            tree = proto.get("tree")
            if tree is None:
                continue
            trees.append((name, ast.Call(
                func=ast.Name(id="Tree"),
                args=[ast.Constant(value=tree["kind"]),
                      ast.Constant(value=tree.get("identity", "")),
                      ast.Dict(keys=[ast.Constant(value=k)
                                     for k in tree["scalars"]],
                               values=[ast.Tuple(elts=[ast.Constant(value=x)
                                                       for x in v])
                                       for v in tree["scalars"].values()]),
                      _walk(tree["list"]), _walk(tree["attrs"])],
                keywords=[])))
    body.append(_table("TREES", "dict[str, Tree]", trees))
    body.append(_table("ASYNC_CLASS", "dict[str, str]", async_of))
    body.extend(_directory(manifest, ordered))
    return ast.unparse(ast.fix_missing_locations(
        ast.Module(body=body, type_ignores=[]))) + "\n"


def unions_module(unions: dict[str, list[str]]) -> str:
    """`_unions.py`: one alias per declared sum type.

    Nothing but aliases, and every one derived from the manifest - so
    the declaration says `DerivedPath = StorePath | DerivedPathBuilt`
    once and this is the same sentence in the package a caller
    imports."""
    # A scalar arm is a builtin, and huggorm_bindings has none to import.
    arms = sorted({a for v in unions.values() for a in v
                   if a not in SCALAR_NAMES})
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
        value: ast.expr = ast.Name(id=python_spelling(members[0]))
        for arm in members[1:]:
            value = ast.BinOp(left=value, op=ast.BitOr(),
                              right=ast.Name(id=python_spelling(arm)))
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




def _adopted(model: ir.Model, m: ir.MethodModel) -> ir.TypeRef | None:
    """The served class a method hands back, to be adopted into its
    async form - itself or `| None` - or None when it returns none."""
    r = m.returns
    if (r is not None and r.kind == "proxy" and r.name in model.served
            and r.origin in ("", "optional")):
        return r.required
    return None


def _as_binding(t: ir.TypeRef) -> tuple[str, str]:
    return t.name, "bindings"


def _as_async(t: ir.TypeRef) -> tuple[str, str]:
    return f"{ASYNC}{t.name}", "siblings"


def _widened(spell: Spelling, model: ir.Model, t: ir.TypeRef) -> str:
    """A constructor's or a free function's parameter: on no protocol,
    so a bare proxy takes the sync object or its async wrapper."""
    if t.kind == "proxy" and not t.origin and t.name in model.served:
        spell.need(t.name, "bindings")
        spell.need(f"{ASYNC}{t.name}", "siblings")
        return f"{t.name} | {ASYNC}{t.name}"
    return spell(t, _as_binding)


def _async_spelling(model: ir.Model, c: ir.ClassModel
                    ) -> tuple[Spelling, list[str],
                               dict[str, tuple[list[str], str]]]:
    """How the in-process async module for `c` spells every type it
    writes: the constructor's parameters, then each method's parameters
    and return.

    A constructor takes the sync object or its async wrapper, and only
    a bare proxy is widened so. A method parameter is the protocol. An
    adopted return is the async class, a type with an async twin is the
    twin, and everything else is itself."""
    spell = Spelling(lambda t: (model.classes[t.name].protocol_name,
                                "protocols"))
    ctor = [_widened(spell, model, p.type) for p in c.ctor]
    methods: dict[str, tuple[list[str], str]] = {}
    for m in c.methods:
        params = [spell(p.type) for p in m.params]
        if _adopted(model, m) is not None:
            ret = spell.returns(m.returns, _as_async)
        elif (twin := model.twins.get(m.return_spelling)) is not None:
            spell.module(twin)
            ret = twin
        else:
            ret = spell.returns(m.returns, _as_binding)
        methods[m.name] = (params, ret)
        spell.defaults(m.params)
    return spell, ctor, methods


def _hop_method(cls: ast.ClassDef, model: ir.Model,
                      m: ir.MethodModel, svc: str,
                      signature: tuple[list[str], str]) -> None:
    """One `async def` that hops to the runner, adopting what it
    returns where the return is a served class."""
    params, returns = signature
    entries = [p.entry() for p in m.params]
    body: list[ast.stmt] = []
    if m.doc:
        body.append(ast.Expr(value=ast.Constant(value=m.doc)))
    if (adopted := _adopted(model, m)) is not None:
        body.append(ast.Assign(
            targets=[ast.Name(id="result")],
            value=ast.Await(value=_hop_call(m.name, entries))))
        wrapped: ast.expr = ast.Call(
            func=ast.Attribute(value=ast.Name(id=f"{ASYNC}{adopted.name}"),
                               attr="_adopt"),
            args=[ast.Name(id="result"),
                  ast.Attribute(value=ast.Name(id="self"), attr="_runner")],
            keywords=[])
        if m.returns is not None and m.returns.optional:
            wrapped = ast.IfExp(
                test=ast.Compare(left=ast.Name(id="result"), ops=[ast.Is()],
                                 comparators=[ast.Constant(value=None)]),
                body=ast.Constant(value=None), orelse=wrapped)
        body.append(ast.Return(value=wrapped))
    elif (twin := model.twins.get(m.return_spelling)) is not None:
        # Same value, other spelling. anyio.Path takes any path-like,
        # so the wrapper constructs one rather than casting: a cast
        # would claim the awaitable methods without adding them.
        body.append(ast.Return(value=ast.Call(
            func=_ann(twin, f"{svc}.{m.name}"),
            args=[ast.Await(value=_hop_call(m.name, entries))],
            keywords=[])))
    else:
        body.append(_hop_return(m.name, entries, m.return_spelling))
    cls.body.append(ast.AsyncFunctionDef(
        name=m.name,
        args=_arguments([ast.arg(arg="self")], entries, params,
                        f"{svc}.{m.name}"),
        body=body,
        decorator_list=[],
        returns=_ann(returns, f"{svc}.{m.name}"),
        type_params=[]))


_POLICY_DOC = {
    "affine": "operations run on the producer's thread.",
    "pool": "operations may run on any pool thread.",
    "inline": "operations run on the calling thread, because none of "
              "them can wait.",
}


def returned_module(model: ir.Model, c: ir.ClassModel) -> ast.Module:
    """Async<X> for a class some call hands back.

    Built with (obj, runner): the object was produced on the producer's
    thread, and `attach_runner` picks the execution its own policy
    says. A returned class can produce another one - a Value holds
    Values - and that return is adopted too."""
    svc = c.name
    policy = c.decl.threading
    execution = c.execution
    spell, _, methods = _async_spelling(model, c)
    # The constructor takes the produced object, typed as what it is.
    spell.bindings.add(svc)

    mod = ast.Module(body=[], type_ignores=[])
    mod.body.append(ast.Expr(value=ast.Constant(value=(
        f"Generated async wrapper for returned type {svc} "
        f"(threading: {policy}) - do not edit."))))
    mod.body.append(_future_annotations())
    typing_names = {"Self"}
    if any(m.returns is not None for m in c.methods):
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
    mod.body.extend(spell.sibling_imports(own=c.async_name))
    mod.body.extend(spell.module_imports())
    mod.body.extend(spell.binding_imports())

    cls = ast.ClassDef(name=c.async_name, bases=[], keywords=[], body=[],
                       decorator_list=[])
    cls.body.append(ast.Expr(value=ast.Constant(value=(
        f"Async handle over a {svc} produced by another wrapper. "
        f"Policy '{policy}': " + _POLICY_DOC[execution]))))
    cls.body.append(ast.Assign(targets=[ast.Name(id="_wire")],
                               value=ast.Constant(value=c.wire)))
    cls.body.append(_runner_decl())
    cls.body.append(ast.FunctionDef(
        name="__init__",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="self"),
                  ast.arg(arg="obj", annotation=_ann(svc, f"{svc}.__init__")),
                  ast.arg(arg="runner",
                          annotation=_ann("BaseRunner", f"{svc}.__init__"))],
            vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None,
            defaults=[]),
        body=[ast.Assign(
            targets=[ast.Attribute(value=ast.Name(id="self"), attr="_runner")],
            value=ast.Call(
                func=ast.Name(id="attach_runner"),
                args=[ast.Name(id="obj"), ast.Name(id="runner"),
                      ast.Constant(value=execution)],
                keywords=[]))],
        decorator_list=[],
        returns=_ann("None", f"{svc}.__init__"),
        type_params=[]))
    cls.body.append(_adopt_method(svc, execution))
    mod.body.append(cls)
    for m in c.methods:
        _hop_method(cls, model, m, svc, methods[m.name])
    cls.body.append(_aclose_method())
    ast.fix_missing_locations(mod)
    return mod


def wrapper_module(model: ir.Model, c: ir.ClassModel) -> ast.Module:
    """Async<X> for a class a caller constructs.

    The object is built lazily, on the runner's own thread, from the
    declared constructor's arguments. A class with no door refuses to
    be built and is only ever received from a call."""
    svc = c.name
    threading = c.decl.threading
    runner = RUNNER_BY_THREADING[threading]
    spell, ctor, methods = _async_spelling(model, c)
    # `_adopt` names the sync class it takes.
    spell.bindings.add(svc)

    mod = ast.Module(body=[], type_ignores=[])
    # Docstring FIRST: anything before it demotes it to a dead
    # expression and leaves the module with no __doc__.
    mod.body.append(ast.Expr(value=ast.Constant(value=(
        f"Generated async wrapper for {svc} (threading: {threading}) - "
        f"do not edit. Built via ast at Nix build time."))))
    mod.body.append(_future_annotations())

    typing_names = {"Self"}
    if not c.constructs:
        typing_names.add("Any")  # the refusing __init__ takes *args/**kwargs
    # A forward hands back Any, and cast is where the declared type is
    # claimed. An adopted return builds a real object instead, and a
    # None return does not return.
    if any(m.returns is not None and _adopted(model, m) is None
           for m in c.methods):
        typing_names.add("cast")
    mod.body.append(ast.ImportFrom(
        module="typing",
        names=[ast.alias(name=n) for n in sorted(typing_names)], level=0))
    mod.body.extend(spell.sibling_imports(own=c.async_name))
    mod.body.extend(spell.module_imports())

    runtime_names = ["BaseRunner", "attach_runner"]
    if c.constructs:
        mod.body.append(ast.ImportFrom(
            module="huggorm_bindings", names=[ast.alias(name=svc)], level=0))
        runtime_names.append(runner)
    mod.body.extend(spell.binding_imports(
        exclude={svc} if c.constructs else ()))
    mod.body.append(ast.ImportFrom(
        module="_runtime",
        names=[ast.alias(name=n) for n in sorted(runtime_names)], level=1))
    if c.ctor and c.constructs:
        # Only the factory calls it, so a constructor taking nothing
        # would leave the import unused - which the smoke gate rejects.
        mod.body.append(ast.ImportFrom(
            module="_runtime", names=[ast.alias(name="unwrap_arg")], level=1))

    cls = ast.ClassDef(name=c.async_name, bases=[], keywords=[], body=[],
                       decorator_list=[])
    cls.body.append(ast.Expr(value=ast.Constant(value=(
        f"Async in-process wrapper over {svc}. The object is constructed "
        f"lazily on its runner thread." if c.constructs else
        f"Async base over {svc}: the surface every subclass guarantees. "
        f"Hold one when you do not care which implementation answered; "
        f"construct a subclass to get one."))))
    cls.body.append(ast.Assign(targets=[ast.Name(id="_wire")],
                               value=ast.Constant(value=c.wire)))
    cls.body.append(_runner_decl())
    cls.body.append(_adopt_method(svc, c.execution))

    if not c.constructs:
        # No runner and no target: there is no way in, so there is
        # nothing to construct lazily. Keyed on the DOOR rather than on
        # `abstract`, which is the C++ fact - nix::Store is abstract and
        # still constructs, through its factory (huggorm#61).
        cls.body.append(ast.FunctionDef(
            name="__init__",
            args=ast.arguments(
                posonlyargs=[], args=[ast.arg(arg="self")],
                vararg=ast.arg(arg="args",
                               annotation=_ann("Any", f"{svc}.__init__")),
                kwonlyargs=[], kw_defaults=[],
                kwarg=ast.arg(arg="kwargs",
                              annotation=_ann("Any", f"{svc}.__init__")),
                defaults=[]),
            body=[ast.Raise(exc=ast.Call(
                func=ast.Name(id="TypeError"),
                args=[ast.Constant(value=(
                    f"{c.async_name} has no constructor: nothing declared "
                    f"makes one. Receive one from a call that returns "
                    f"{svc}."))],
                keywords=[]))],
            decorator_list=[], returns=_ann("None", f"{svc}.__init__"),
            type_params=[]))
    else:
        init_kwargs = []
        if threading == "affine":
            init_kwargs.append(ast.keyword(
                arg="name", value=ast.Constant(value=f"huggorm-affine-{svc}")))
        entries = [p.entry() for p in c.ctor]
        # A zero-argument lambda over __init__'s parameters, so the
        # object is built on the runner's thread, not the caller's.
        # Each argument goes through unwrap_arg: a wrapper passed in
        # contributes its target object, not the async shell.
        factory = ast.Lambda(
            args=ast.arguments(posonlyargs=[], args=[], vararg=None,
                               kwonlyargs=[], kw_defaults=[], kwarg=None,
                               defaults=[]),
            body=ast.Call(
                func=ast.Name(id=svc),
                args=[ast.Call(func=ast.Name(id="unwrap_arg"),
                               args=[ast.Name(id=p.name)], keywords=[])
                      for p in c.ctor],
                keywords=[]))
        cls.body.append(ast.FunctionDef(
            name="__init__",
            args=_arguments([ast.arg(arg="self")], entries, ctor,
                            f"{svc}.__init__"),
            body=[ast.Assign(
                targets=[ast.Attribute(value=ast.Name(id="self"),
                                       attr="_runner")],
                value=ast.Call(func=ast.Name(id=runner), args=[factory],
                               keywords=init_kwargs))],
            decorator_list=[],
            returns=_ann("None", f"{svc}.__init__"),
            type_params=[]))

    for m in c.methods:
        _hop_method(cls, model, m, svc, methods[m.name])
    cls.body.append(_aclose_method())
    mod.body.append(cls)
    ast.fix_missing_locations(mod)
    return mod




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


def _adopt_method(svc: str, execution: str) -> ast.FunctionDef:
    """`Async<svc>._adopt(obj, runner)`: an object some call produced,
    in its async form.

    Every served class has it, and every adoption calls it. A wrapper
    class's `__init__` is its constructor and takes the constructor's
    arguments, so adoption cannot go through `__init__`; `__new__`
    skips it, and `attach_runner` picks the runner from the class's
    own execution policy."""
    where = f"{svc}._adopt"
    return ast.FunctionDef(
        name="_adopt",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="cls"),
                  ast.arg(arg="obj", annotation=_ann(svc, where)),
                  ast.arg(arg="runner", annotation=_ann("BaseRunner", where))],
            vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None,
            defaults=[]),
        body=[
            ast.Assign(
                targets=[ast.Name(id="adopted")],
                value=ast.Call(
                    func=ast.Attribute(value=ast.Name(id="cls"), attr="__new__"),
                    args=[ast.Name(id="cls")], keywords=[])),
            ast.Assign(
                targets=[ast.Attribute(value=ast.Name(id="adopted"),
                                       attr="_runner")],
                value=ast.Call(
                    func=ast.Name(id="attach_runner"),
                    args=[ast.Name(id="obj"), ast.Name(id="runner"),
                          ast.Constant(value=execution)],
                    keywords=[])),
            ast.Return(value=ast.Name(id="adopted")),
        ],
        decorator_list=[ast.Name(id="classmethod")],
        returns=_ann("Self", where),
        type_params=[])






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
    defers evaluation on 3.14; the package also runs on 3.11 and up,
    where an async wrapper's method returning its own class needs this
    (huggorm#107)."""
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


def _params(m: Proto, cls_name: str,
            spell: Callable[[str], str] = str) -> ast.arguments:
    """`self` plus one typed argument per declared parameter. `spell`
    turns a declared type into the annotation this module writes."""
    return _arguments(
        [ast.arg(arg="self")], m["params"],
        [spell(p["type"]) for p in m["params"]],
        f"{cls_name}.{m['name']}")


def protocol_module(model: ir.Model) -> ast.Module:
    """Emit one Protocol per served class: the surface a caller can
    program against without knowing whether the object answering is in
    this process or on the far side of a socket.

    A method with no rpc is absent, and the manifest says why. A proxy,
    parameter or return, is spelled as its protocol, here and on both
    implementations."""
    from huggorm_gen.pygen.surface import ACLOSE

    spell = Spelling(lambda t: (model.classes[t.name].protocol_name,
                                "defined"))
    # Spelled once, before the module is written: the imports come
    # first in the file and only the spelling knows what they are.
    signatures = {
        (cls.name, m.name): ([spell(p.type) for p in m.params],
                             spell.returns(m.returns))
        for cls in model.ordered_served for m in cls.methods
        if model.offered(m)
    }
    for served in model.ordered_served:
        for m in served.methods:
            if model.offered(m):
                spell.defaults(m.params)

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
    mod.body.extend(spell.imports())

    for model_cls in model.ordered_served:
        name = model_cls.name
        cls = ast.ClassDef(
            name=model_cls.protocol_name, bases=[ast.Name(id="Protocol")],
            keywords=[], body=[],
            # isinstance() against this checks that the method NAMES are
            # present and nothing more. The signature gate in the smoke
            # test is what proves an implementation really conforms.
            decorator_list=[ast.Name(id="runtime_checkable")],
            type_params=[])
        withheld = [m.name for m in model_cls.methods
                    if not model.offered(m)]
        cls.body.append(ast.Expr(value=ast.Constant(value=(
            f"What every {name} implementation promises."
            + (f" {', '.join(sorted(withheld))} cannot be promised: see "
               f"protocol_blockers in the manifest." if withheld else "")))))
        for m in model_cls.methods:
            if not model.offered(m):
                continue
            params, returns = signatures[(name, m.name)]
            body: list[ast.stmt] = []
            if m.doc:
                body.append(ast.Expr(value=ast.Constant(value=m.doc)))
            body.append(ast.Expr(value=ast.Constant(value=Ellipsis)))
            cls.body.append(ast.AsyncFunctionDef(
                name=m.name,
                args=_arguments([ast.arg(arg="self")],
                                [p.entry() for p in m.params], params,
                                f"{name}.{m.name}"),
                body=body,
                decorator_list=[],
                returns=_ann(returns, f"{name}.{m.name}"),
                type_params=[]))
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
        args=[ast.Constant(value=m["name"]),
              ast.Constant(value=rpc["path"]),
              ast.Constant(value=rpc["req"]),
              ast.Constant(value=rpc["resp"]),
              _args(m["params"]),
              ast.Constant(value=m["return_type"])],
        keywords=[])


def _directory(manifest: Proto, ordered: list[Proto]) -> list[ast.stmt]:
    """The three tables a caller reaches BY NAME.

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
                                         if p["default"] is None)),
                  ast.Tuple(elts=[ast.Constant(value=p["name"]) for p in ctor
                                  if p["default"] == "None"])],
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
    return [_table("ACQUIRE", "dict[str, Acquire]", acquires),
            _table("FREE", "dict[str, Call]", free),
            _table("NO_RPC", "dict[str, str]", blocked)]


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


def rpc_module(model: ir.Model) -> ast.Module:
    """Emit one RPC client class per served class.

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
    from huggorm_gen.pygen.surface import ACLOSE, REGISTRY

    ordered = model.ordered_served
    # A returned proxy is an RPC class: the server leased a handle. A
    # proxy PARAMETER is spelled as the protocol, as on every surface,
    # and the client refuses an in-process object when it encodes one.
    spell = Spelling(lambda t: (model.classes[t.name].protocol_name,
                                "protocols"))
    returned = Spelling(lambda t: (model.classes[t.name].rpc_name,
                                   "defined"))
    # Only the methods this module WRITES, so a type named only by a
    # method with no rpc does not become an unused import.
    signatures = {
        (cls.name, m.name): ([spell(p.type) for p in m.params],
                             returned.returns(m.returns))
        for cls in ordered for m in cls.methods if model.offered(m)
    }
    for served_cls in ordered:
        for m in served_cls.methods:
            if model.offered(m):
                spell.defaults(m.params)
    spell.absorb(returned)

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
    # The call specs, from where the build wrote them. Emitted in
    # `_policy` rather than here, because the SERVER reads the same
    # ones - and two derivations of one call's shape is exactly the
    # disagreement this repo generates code to prevent.
    specs = sorted(_spec_name(name, m) for name, m in signatures)
    mod.body.append(ast.ImportFrom(
        module="._callspec", names=[ast.alias(name="Call")], level=0))
    if specs:
        mod.body.append(ast.ImportFrom(
            module="._policy",
            names=[ast.alias(name=s) for s in specs], level=0))
    mod.body.extend(spell.imports())

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

    for served_cls in ordered:
        name = served_cls.name
        cls = ast.ClassDef(
            name=served_cls.rpc_name, bases=[],
            keywords=[], body=[], decorator_list=[], type_params=[])
        cls.body.append(ast.Expr(value=ast.Constant(value=(
            f"A {name} living behind a handle on a server. Same surface as "
            f"{served_cls.async_name}, different location."))))
        cls.body.append(ast.Assign(targets=[ast.Name(id="_wire")],
                                   value=ast.Constant(value=served_cls.wire)))
        for attr, kind in (("_client", CLIENT_PROTOCOL),
                           ("handle_id", "str | None")):
            cls.body.append(ast.AnnAssign(
                target=ast.Name(id=attr),
                annotation=_ann(kind, f"{name}.{attr}"),
                value=None, simple=1))

        # Only the methods that HAVE an rpc. A method the wire cannot
        # carry keeps its in-process wrapper and is simply absent here;
        # the manifest says why, and the protocol drops it too.
        callable_ = [m for m in served_cls.methods if model.offered(m)]

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
            params, returns = signatures[(name, m.name)]
            body: list[ast.stmt] = []
            if m.doc:
                body.append(ast.Expr(value=ast.Constant(value=m.doc)))
            body.append(_forward(ast.Call(
                func=ast.Attribute(
                    value=ast.Attribute(value=ast.Name(id="self"),
                                        attr="_client"),
                    attr="invoke"),
                args=[
                    ast.Name(id=_spec_name(name, m.name)),
                    ast.Attribute(value=ast.Name(id="self"), attr="handle_id"),
                    ast.List(elts=[ast.Name(id=p.name) for p in m.params]),
                ],
                keywords=[]), returns))
            cls.body.append(ast.AsyncFunctionDef(
                name=m.name,
                args=_arguments([ast.arg(arg="self")],
                                [p.entry() for p in m.params], params,
                                f"{name}.{m.name}"),
                body=body,
                decorator_list=[],
                returns=_ann(returns, f"{name}.{m.name}"),
                type_params=[]))

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
            keys=[ast.Constant(value=c.name) for c in ordered],
            values=[ast.Name(id=c.rpc_name) for c in ordered]),
        simple=1))
    ast.fix_missing_locations(mod)
    return mod


FREE_MODULE = "free_functions"


def free_function_module(model: ir.Model) -> ast.Module:
    """Module-level coroutines for the bindings' wrapped free functions.

    No instance, so no runner to hop through and no handle to hold:
    the shared pool, which is what "pool" means everywhere else. The
    sync function is imported under an underscore alias so the
    coroutine can take its plain name.

    A returned served class is adopted through `_adopt`, as a method's
    return is. Only a POOL class can be: an affine one needs a home
    thread and a free function has none. Refused here, at generation,
    because the alternative is a coroutine that hands a sync object to
    an async caller and a server that leases one."""
    fns = sorted((f for f in model.functions.values() if f.wrapped),
                 key=lambda f: f.name)
    pool_parent = False
    for fn in fns:
        r = fn.returns
        adopted = (r.required if r is not None and r.kind == "proxy"
                   and r.origin in ("", "optional") else None)
        if adopted is None:
            if r is not None and r.leaf.kind == "proxy":
                raise ValueError(
                    f"free function {fn.name} returns {r.spelling}, and "
                    f"{r.leaf.name} cannot be adopted into an async form "
                    f"from a free function. Return a served class on its "
                    f"own or as `X | None`.")
            continue
        policy = model.classes[adopted.name].decl.threading
        if policy != "pool":
            raise ValueError(
                f"free function {fn.name} returns {adopted.name}, which is "
                f"{policy}: it needs a home thread and a free function has "
                f"none. Return it from a method of the class that owns the "
                f"thread instead.")
        pool_parent = True

    spell = Spelling()
    signatures = {}
    for fn in fns:
        params = [_widened(spell, model, p.type) for p in fn.params]
        r = fn.returns
        ret = (spell.returns(r, _as_async)
               if r is not None and r.kind == "proxy"
               else spell.returns(r, _as_binding))
        signatures[fn.name] = (params, ret)
        spell.defaults(fn.params)

    mod = ast.Module(body=[], type_ignores=[])
    mod.body.append(ast.Expr(value=ast.Constant(
        value="Generated async wrappers for the bindings' module-level "
              "functions - do not edit. Built via ast at Nix build time.")))
    mod.body.append(_future_annotations())
    if any(f.returns is not None and f.returns.kind != "proxy" for f in fns):
        mod.body.append(ast.ImportFrom(
            module="typing", names=[ast.alias(name="cast")], level=0))
    mod.body.extend(spell.sibling_imports())
    mod.body.extend(spell.module_imports())
    mod.body.extend(spell.binding_imports())
    mod.body.append(ast.ImportFrom(
        module="huggorm_bindings",
        names=[ast.alias(name=f.name, asname="_" + f.name)
               for f in sorted(fns, key=lambda x: x.name)],
        level=0))
    mod.body.append(ast.ImportFrom(
        module="_runtime",
        names=[ast.alias(name="call_function")]
        + ([ast.alias(name="PoolRunner")] if pool_parent else []),
        level=1))

    for fn in fns:
        params, ret = signatures[fn.name]
        entries = [p.entry() for p in fn.params]
        body: list[ast.stmt] = []
        if fn.doc:
            body.append(ast.Expr(value=ast.Constant(value=fn.doc)))
        call = ast.Call(
            func=ast.Name(id="call_function"),
            args=[ast.Name(id="_" + fn.name),
                  ast.List(elts=[ast.Name(id=p.name) for p in fn.params])],
            keywords=[])
        r = fn.returns
        if r is not None and r.kind == "proxy":
            body.append(ast.Assign(targets=[ast.Name(id="result")],
                                   value=ast.Await(value=call)))
            # A pool policy ignores the parent, so a fresh PoolRunner
            # stands in for the producer a method would pass.
            wrapped: ast.expr = ast.Call(
                func=ast.Attribute(value=ast.Name(id=f"{ASYNC}{r.name}"),
                                   attr="_adopt"),
                args=[ast.Name(id="result"),
                      ast.Call(func=ast.Name(id="PoolRunner"),
                               args=[ast.Constant(value=None)], keywords=[])],
                keywords=[])
            if r.optional:
                wrapped = ast.IfExp(
                    test=ast.Compare(left=ast.Name(id="result"),
                                     ops=[ast.Is()],
                                     comparators=[ast.Constant(value=None)]),
                    body=ast.Constant(value=None), orelse=wrapped)
            body.append(ast.Return(value=wrapped))
        else:
            body.append(_forward(call, fn.returns.spelling
                                 if fn.returns is not None else "None"))
        mod.body.append(ast.AsyncFunctionDef(
            name=fn.name,
            args=_arguments([], entries, params, fn.name),
            body=body,
            decorator_list=[],
            returns=_ann(ret, fn.name),
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
    `sorted(paths)` an error on a list of them (huggorm#46).

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
    (huggorm#27).

    Everything here already exists in the protocol dicts: every
    method signature and every constructor signature, as the
    declaration wrote them.

    `produced` names the classes that are handed back rather than
    constructed. Their __init__ raises unconditionally, so the stub
    says NoReturn - true, and it makes StorePath() an error at the call
    site instead of a TypeError at runtime.

    These protos are the UNFILTERED ones. The generated surface is not
    the binding surface: 018 moves the shared methods off the
    subclasses, which is a rule about the async wrappers. A stub that
    described the sync bindings that way would hide a method the
    binding has."""
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
                name="__init__",
                args=_arguments([ast.arg(arg="self")], proto["ctor"],
                                [p["type"] for p in proto["ctor"]],
                                f"{name}.__init__"),
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
                name=m["name"], args=_params(m, name),
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
                elts=[ast.Constant(value=n)
                      for n in package_exports(all_names, free_names)]),
        )
    )
    ast.fix_missing_locations(mod)
    return mod


def package_exports(all_names: list[str],
                    free_names: list[str] | None = None) -> list[str]:
    """Everything `huggorm_generated` offers, in `__all__` order.

    Apart from `init_module` because a second file needs the same
    list: `huggorm.__init__` re-exports this package whole, and the
    front door is emitted too (huggorm#64). Computing it there as well
    would be one list stated twice, which is exactly the thing that
    front door existed as.

    `RPC_CLASSES` is in it. It is a registry the client uses to turn a
    handle into an object rather than surface, and the front door
    drops it - but this package does export it, and saying otherwise
    here would be a lie a reader of `__all__` could measure."""
    from huggorm_gen.pygen.surface import (
        REGISTRY,
        async_class_name,
        protocol_name,
        rpc_class_name,
    )

    return ([f(n) for n in all_names
             for f in (async_class_name, protocol_name, rpc_class_name)]
            + [REGISTRY] + list(free_names or []))
