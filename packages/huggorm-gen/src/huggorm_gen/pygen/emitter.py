"""Emit ast trees from the typed model (`huggorm_gen.ir`).

Pure tree building - no I/O. Everything an emitter needs arrives in
the model.
"""

import ast
import dataclasses
import textwrap
from collections.abc import Mapping, Sequence
from string import Template

from huggorm_gen import ir
from huggorm_gen.payload import callspec as cs
from huggorm_gen.payload.wiretypes import (
    python_spelling,
)
from huggorm_gen.pygen import grpc_schema
from huggorm_gen.pygen.spell import (
    BINDINGS,
    PROTOCOLS,
    Source,
    Spelling,
    import_from,
    sibling,
)

ASYNC = ir.ASYNC

def _code(src: str, signature: ast.arguments | None = None,
          **subst: str) -> ast.stmt:
    """One statement from a source template.

    `$name` is replaced as text and the result is parsed, so a bad
    substitution fails here. `signature` replaces a def's parameters,
    which `_arguments` spells per surface."""
    node = ast.parse(Template(textwrap.dedent(src)).substitute(subst)).body[0]
    if signature is not None:
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        node.args = signature
    return node


def _def(header: str, body: str = "...", doc: str = "",
         signature: ast.arguments | None = None) -> ast.stmt:
    """One def from its header and its body, both as source.

    `signature` replaces the parameters, for one `_arguments` spells
    per surface, and `doc` goes first when there is one."""
    indented = textwrap.indent(textwrap.dedent(body).strip(), "    ")
    src = f"{header}:\n{indented}"
    try:
        node = ast.parse(src).body[0]
    except SyntaxError as e:
        raise ValueError(f"cannot parse the emitted {header!r}") from e
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    if signature is not None:
        node.args = signature
    if doc:
        node.body.insert(0, ast.Expr(value=ast.Constant(value=doc)))
    return node


def _forwarded(call: str, returns: str) -> str:
    """The body that hands back an awaited forward, typed.

    The runtime hands back Any - it dispatches by method name onto an
    object it knows nothing about. The declared type is the
    declaration's claim about that method, so the cast is where the
    claim is made rather than a silent Any leaking into every caller.
    A method returning None does not return at all: there is nothing
    to hand back."""
    if returns == "None":
        return f"await {call}"
    return f"return cast({returns}, await {call})"


def _adopting(call: str, async_name: str, runner: str, optional: bool) -> str:
    """The body that adopts what a call hands back into its async form,
    and passes None through when the return may be None."""
    adopt = f"{async_name}._adopt(result, {runner})"
    if optional:
        adopt = f"None if result is None else {adopt}"
    return f"result = await {call}\nreturn {adopt}"


RUNNER_BY_THREADING = {
    "affine": "AffineRunner",
    "pool": "PoolRunner",
}

def _arguments(leading: list[ast.arg], params: Sequence[ir.ParamModel],
               types: list[str], where: str) -> ast.arguments:
    """`leading` plus one argument per declared parameter, annotated
    and defaulted.

    `types` is the annotation each parameter is written with. The
    caller resolves it, because the same declared type is spelled
    differently in an async wrapper, in a protocol and in a stub.

    The default is written from the declared source string, so every
    surface offers the same one. A caller that omits the argument gets
    the same value in-process and over RPC, and the wire never has to
    represent absence."""
    args = list(leading)
    defaults: list[ast.expr] = []
    for p, type_str in zip(params, types, strict=True):
        annotation = _ann(type_str, f"{where}:{p.name}")
        if p.default == "None" and not _admits_none(annotation):
            # A parameter that may be omitted is spelled `T | None`.
            # `output: str = None` is implicit Optional, which strict
            # typecheckers reject and which misdescribes the default
            # the emitter itself writes. Here rather than at each call
            # site: the four surfaces spell the TYPE differently and
            # none of them spells this differently.
            annotation = ast.BinOp(left=annotation, op=ast.BitOr(),
                                   right=ast.Constant(value=None))
        args.append(ast.arg(arg=p.name, annotation=annotation))
        if p.default is not None:
            defaults.append(_ann(p.default, f"{where}:{p.name}="))
        elif defaults:
            raise ValueError(
                f"{where}: required parameter {p.name!r} follows a "
                f"defaulted one")
    return ast.arguments(posonlyargs=[], args=args, vararg=None,
                         kwonlyargs=[], kw_defaults=[], kwarg=None,
                         defaults=defaults)


def _admits_none(annotation: ast.expr) -> bool:
    """Whether a union arm of `annotation` is already None."""
    if isinstance(annotation, ast.Constant):
        return annotation.value is None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _admits_none(annotation.left) or _admits_none(annotation.right)
    return False


# The emitted `_policy.py`'s own docstring. Out here rather than
# inline, because it is prose a reader of the OUTPUT sees and an
# 800-column string literal in an emitter argument list is neither
# readable nor lintable.
# The two modules every served class has a form in, beside its own
# `async_<name>` module.
PROTOCOL_MODULE = "protocols"
RPC_MODULE = "rpc"

# Every implementation closes the same way, and only the meaning
# differs: in process it shuts the runner's thread down, remotely it
# gives the lease back. So it belongs on the protocol, and it is the
# one method no binding declares.
ACLOSE = "aclose"

# The registry a client uses to turn a handle into an object of the
# right class.
REGISTRY = "RPC_CLASSES"

POLICY_DOC = """The wire policy of every declared type.

The tables the codec needs and no caller does: what a wire value and
an error are made of, a sum type's arms, and every call's spec. Each
type is a `Wire`, resolved by the build, so the codec parses nothing.

Arms in DECLARED order, because that is the order the schema numbered
the oneof's fields in and a renumbering is a wire change. Tuples
rather than lists, so nothing downstream reorders one in place."""


def _literal(v: object) -> ast.expr:
    """A value as the source that rebuilds it.

    A dataclass is called positionally until a field holds its
    default; each later field that differs is a keyword. So
    `Wire("list", item=...)` leaves `name` out, and the emitted call
    is the one a person would write."""
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        args: list[ast.expr] = []
        keywords: list[ast.keyword] = []
        for f in dataclasses.fields(v):
            x = getattr(v, f.name)
            if f.default is not dataclasses.MISSING and x == f.default:
                continue
            if keywords or len(args) < [g.name for g in dataclasses.fields(v)].index(f.name):
                keywords.append(ast.keyword(arg=f.name, value=_literal(x)))
            else:
                args.append(_literal(x))
        return ast.Call(func=ast.Name(id=type(v).__name__), args=args,
                        keywords=keywords)
    if isinstance(v, tuple):
        return ast.Tuple(elts=[_literal(x) for x in v])
    if isinstance(v, dict):
        return ast.Dict(keys=[_literal(k) for k in v],
                        values=[_literal(x) for x in v.values()])
    if v is None or isinstance(v, (str, int, float)):
        return ast.Constant(value=v)
    raise TypeError(f"no literal for {type(v).__name__}: {v!r}")


def _table(var: str, ann: str, rows: Sequence[tuple[str, object]]) -> ast.stmt:
    """One emitted lookup table, named and annotated. A row's value is
    an expression already, or a value `_literal` writes."""
    return ast.AnnAssign(
        target=ast.Name(id=var), annotation=_ann(ann, var),
        value=ast.Dict(keys=[ast.Constant(value=n) for n, _ in rows],
                       values=[v if isinstance(v, ast.expr) else _literal(v)
                               for _, v in rows]),
        simple=1)


def _wire(t: ir.TypeRef | None) -> cs.Wire | None:
    """A resolved type as the `Wire` the codec dispatches on."""
    if t is None:
        return None
    optional = t.optional
    t = t.required
    if t.container:
        return cs.Wire("list" if t.origin == "list" else "map",
                       item=_wire(t.args[0]), optional=optional)
    if t.scalar is not None:
        # The leaf's own name, not the builtin it goes in as: the codec
        # converts a `datetime.timedelta` by name.
        return cs.Wire("scalar", t.width or t.name, optional=optional)
    if t.kind in ("enum", "value", "union", "error", "proxy"):
        return cs.Wire(t.kind, t.name, optional=optional)
    raise TypeError(f"{t.spelling} is a {t.kind}, which does not cross")


def _args(pairs: Sequence[tuple[str, ir.TypeRef]]) -> tuple[cs.Arg, ...]:
    """Named, typed parts - wire fields or parameters - as `Arg`s."""
    out = []
    for name, t in pairs:
        w = _wire(t)
        assert w is not None
        out.append(cs.Arg(name, w))
    return tuple(out)


def policy_module(model: ir.Model) -> str:
    """`_policy.py`: the wire policy of every declared type.

    The tables the codec and the server read: what a wire value and an
    error are made of, a sum type's arms, every call's spec, the value
    trees, and the directory a caller reaches by name. Each value is
    the `_callspec` dataclass itself, built here and written by
    `_literal`, so a wrong shape fails in this build.

    An emitted module, not data loaded at run time: a typechecker sees
    the type of every table. The module ships with the code that reads
    it, so no other generator can supply it, and nothing checks it at
    load time.

    Arms in DECLARED order, because that is the order the schema
    numbered the oneof's fields in and a renumbering is a wire change.
    A tuple rather than a list, so nothing downstream can reorder them
    in place.
    """
    classes = [*model.constructed, *model.handed_back]
    body: list[ast.stmt] = [
        ast.Expr(value=ast.Constant(value=POLICY_DOC)),
        import_from("_callspec", "Acquire", "Arg", "Call", "Tree", "Walk", "Wire",
                    level=1),
    ]
    # The protobuf package every message and service sits in.
    body.append(ast.AnnAssign(
        target=ast.Name(id="PKG"), annotation=_ann("str", "PKG"),
        value=ast.Constant(value=ir.PROTO_PACKAGE), simple=1))
    body.append(_table("WIRE_FIELDS", "dict[str, tuple[Arg, ...]]",
                       [(c.name, _args([(f.name, f.type) for f in c.wire_fields]))
                        for c in classes if c.wire == "value"]))
    # The exception surface. ERROR_MODULE is where the emitted module
    # lands, which the fault codec imports to construct one; the
    # fields are what it is rebuilt FROM.
    body.append(ast.AnnAssign(
        target=ast.Name(id="ERROR_MODULE"), annotation=_ann("str", "ERROR_MODULE"),
        value=ast.Constant(value=model.errors.module), simple=1))
    body.append(_table("ERROR_FIELDS", "dict[str, tuple[Arg, ...]]", [
        (n, _args([(f.name, f.type) for f in e.wire_fields]))
        for n, e in model.errors.classes.items()]))
    body.append(ast.AnnAssign(
        target=ast.Name(id="LOG_RECORDS"), annotation=_ann("Wire", "LOG_RECORDS"),
        value=_literal(_wire(grpc_schema.LOG_RECORDS)), simple=1))
    body.append(_table("UNION_ARMS", "dict[str, tuple[Wire, ...]]", [
        (n, tuple(_wire(a) for a in arms))
        for n, arms in model.unions.items()]))
    # Every method's call spec, ONCE. The client reads these through
    # `rpc.py` and the server reads them through METHODS below, so the
    # two ends of a call cannot disagree about its shape.
    methods = []
    for c in model.ordered_served:
        names = []
        for m in c.methods:
            if not model.offered(m):
                continue
            var = _spec_name(c.name, m.name)
            body.append(ast.Assign(
                targets=[ast.Name(id=var)],
                value=_literal(_spec(m.name, c.rpc(m), m.params, m.returns))))
            names.append(var)
        methods.append((c.name, ast.Tuple(
            elts=[ast.Name(id=n) for n in names])))
    body.append(_table("METHODS", "dict[str, tuple[Call, ...]]", methods))
    # The value TREES, and the async class each served class is adopted
    # into. SERVED, not merely wrapped: an unserved class has no async
    # class emitted, and `server.adopt` would raise AttributeError on
    # its first handle (huggorm#32).
    body.append(_table("TREES", "dict[str, Tree]", [
        (c.name, c.tree) for c in classes if c.tree is not None]))
    body.append(_table("ASYNC_CLASS", "dict[str, str]", [
        (c.name, c.async_name)
        for c in classes if c.served]))
    body.extend(_directory(model))
    return ast.unparse(ast.fix_missing_locations(
        ast.Module(body=body, type_ignores=[]))) + "\n"


def unions_module(unions: Mapping[str, Sequence[ir.TypeRef]]) -> str:
    """`_unions.py`: one alias per declared sum type.

    Nothing but aliases, and every one derived from the model - so
    the declaration says `DerivedPath = StorePath | DerivedPathBuilt`
    once and this is the same sentence in the package a caller
    imports."""
    # A scalar arm is a builtin, and huggorm_bindings has none to import.
    arms = sorted({a.name for v in unions.values() for a in v
                   if a.kind != "scalar"})
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
        value: ast.expr = ast.Name(id=python_spelling(members[0].name))
        for arm in members[1:]:
            value = ast.BinOp(left=value, op=ast.BitOr(),
                              right=ast.Name(id=python_spelling(arm.name)))
        body.append(ast.Assign(targets=[ast.Name(id=alias)], value=value))
    body.append(ast.Assign(
        targets=[ast.Name(id="__all__")],
        value=ast.List(elts=[ast.Constant(value=a) for a in unions])))
    return ast.unparse(ast.fix_missing_locations(
        ast.Module(body=body, type_ignores=[]))) + "\n"


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


def _as_binding(t: ir.TypeRef) -> tuple[str, Source | None]:
    return t.name, BINDINGS


def _as_async(t: ir.TypeRef) -> tuple[str, Source | None]:
    return f"{ASYNC}{t.name}", sibling(t.name)


def _widened(spell: Spelling, model: ir.Model, t: ir.TypeRef) -> str:
    """A constructor's or a free function's parameter: on no protocol,
    so a bare proxy takes the sync object or its async wrapper."""
    if t.kind == "proxy" and not t.origin and t.name in model.served:
        spell.need(t.name, BINDINGS)
        spell.need(f"{ASYNC}{t.name}", sibling(t.name))
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
                                PROTOCOLS))
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
    call = (f"self._runner.call({m.name!r}, "
            f"[{', '.join(p.name for p in m.params)}])")
    if (adopted := _adopted(model, m)) is not None:
        body = _adopting(call, f"{ASYNC}{adopted.name}", "self._runner",
                         m.returns is not None and m.returns.optional)
    elif (twin := model.twins.get(m.return_spelling)) is not None:
        # Same value, other spelling. anyio.Path takes any path-like,
        # so the wrapper constructs one rather than casting: a cast
        # would claim the awaitable methods without adding them.
        body = f"return {twin}(await {call})"
    else:
        body = _forwarded(call, m.return_spelling)
    cls.body.append(_def(
        f"async def {m.name}() -> {returns}", body, m.doc,
        _arguments([ast.arg(arg="self")], m.params, params, f"{svc}.{m.name}")))


_POLICY_DOC = {
    "affine": "operations run on the producer's thread.",
    "pool": "operations may run on any pool thread.",
    "inline": "operations run on the calling thread, because none of "
              "them can wait.",
}


def _async_module(model: ir.Model, c: ir.ClassModel, doc: str,
                  class_doc: str, init: ast.stmt, typing_names: set[str],
                  runtime_names: set[str]) -> ast.Module:
    """`Async<X>`: the skeleton both async modules share. `init` is
    how an instance comes to hold an object."""
    spell, _, methods = _async_spelling(model, c)
    # `__init__` or `_adopt` names the sync class it takes.
    spell.need(c.name, BINDINGS)
    cls = ast.ClassDef(name=c.async_name, bases=[], keywords=[], decorator_list=[], body=[
        ast.Expr(value=ast.Constant(value=class_doc)),
        _code("_wire = $wire", wire=repr(c.wire)),
        _code("_runner: BaseRunner"),
        init,
        _code("""
            @classmethod
            def _adopt(cls, obj: $svc, runner: BaseRunner) -> Self:
                adopted = cls.__new__(cls)
                adopted._runner = attach_runner(obj, runner, $execution)
                return adopted
            """, svc=c.name, execution=repr(c.execution)),
    ])
    for m in c.methods:
        _hop_method(cls, model, m, c.name, methods[m.name])
    cls.body.append(_code("""
        async def aclose(self) -> None:
            await self._runner.aclose()
        """))
    # A forward hands back Any, and cast is where the declared type is
    # claimed. An adopted return builds a real object instead, and a
    # None return does not return.
    if any(m.returns is not None and _adopted(model, m) is None
           for m in c.methods):
        typing_names = typing_names | {"cast"}
    # Docstring FIRST: anything before it demotes it to a dead
    # expression and leaves the module with no __doc__.
    mod = ast.Module(type_ignores=[], body=[
        ast.Expr(value=ast.Constant(value=doc)),
        _future_annotations(),
        import_from("typing", "Self", *typing_names),
        import_from("_runtime", "BaseRunner", "attach_runner", *runtime_names,
                    level=1),
        # A value that produces values names its OWN async class, which
        # is defined right here: importing it would be a self-import.
        *spell.imports(own=c.async_name),
        cls,
    ])
    ast.fix_missing_locations(mod)
    return mod


def returned_module(model: ir.Model, c: ir.ClassModel) -> ast.Module:
    """Async<X> for a class some call hands back.

    Built with (obj, runner): the object was produced on the producer's
    thread, and `attach_runner` picks the execution its own policy
    says. A returned class can produce another one - a Value holds
    Values - and that return is adopted too."""
    policy = c.decl.threading
    init = _code("""
        def __init__(self, obj: $svc, runner: BaseRunner) -> None:
            self._runner = attach_runner(obj, runner, $execution)
        """, svc=c.name, execution=repr(c.execution))
    return _async_module(
        model, c,
        f"Generated async wrapper for returned type {c.name} "
        f"(threading: {policy}) - do not edit.",
        f"Async handle over a {c.name} produced by another wrapper. "
        f"Policy '{policy}': " + _POLICY_DOC[c.execution],
        init, set(), set())


def wrapper_module(model: ir.Model, c: ir.ClassModel) -> ast.Module:
    """Async<X> for a class a caller constructs.

    The object is built lazily, on the runner's own thread, from the
    declared constructor's arguments. A class with no door refuses to
    be built and is only ever received from a call."""
    svc = c.name
    threading = c.decl.threading
    doc = (f"Generated async wrapper for {svc} (threading: {threading}) - "
           f"do not edit. Built via ast at Nix build time.")
    if not c.constructs:
        # No runner and no target: there is no way in, so there is
        # nothing to construct lazily. Keyed on the DOOR rather than on
        # `abstract`, which is the C++ fact - nix::Store is abstract and
        # still constructs, through its factory (huggorm#61).
        init = _code("""
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise TypeError($message)
            """, message=repr(
                f"{c.async_name} has no constructor: nothing declared makes "
                f"one. Receive one from a call that returns {svc}."))
        return _async_module(
            model, c, doc,
            f"Async base over {svc}: the surface every subclass guarantees. "
            f"Hold one when you do not care which implementation answered; "
            f"construct a subclass to get one.",
            init, {"Any"}, set())
    runner = RUNNER_BY_THREADING[threading]
    name = (f", name={f'huggorm-affine-{svc}'!r}" if threading == "affine"
            else "")
    _, ctor, _ = _async_spelling(model, c)
    # A zero-argument lambda over __init__'s parameters, so the object
    # is built on the runner's thread, not the caller's. Each argument
    # goes through unwrap_arg: a wrapper passed in contributes its
    # target object, not the async shell.
    init = _code("""
        def __init__(self) -> None:
            self._runner = $runner(lambda: $svc($values)$name)
        """, _arguments([ast.arg(arg="self")], c.ctor, ctor, f"{svc}.__init__"),
        runner=runner, svc=svc, name=name,
        values=", ".join(f"unwrap_arg({p.name})" for p in c.ctor))
    # `unwrap_arg` only when the factory calls it: an unused import is
    # what the smoke gate rejects.
    return _async_module(
        model, c, doc,
        f"Async in-process wrapper over {svc}. The object is constructed "
        f"lazily on its runner thread.",
        init, set(), {runner} | ({"unwrap_arg"} if c.ctor else set()))


def _future_annotations() -> ast.ImportFrom:
    """Lazy annotations, so a class may name one defined further down.

    The protocol and rpc modules each hold the whole surface, and a
    method on the first class can return the last one. PEP 649 already
    defers evaluation on 3.14; the package also runs on 3.11 and up,
    where an async wrapper's method returning its own class needs this
    (huggorm#107)."""
    return ast.ImportFrom(module="__future__",
                          names=[ast.alias(name="annotations")], level=0)


def protocol_module(model: ir.Model) -> ast.Module:
    """Emit one Protocol per served class: the surface a caller can
    program against without knowing whether the object answering is in
    this process or on the far side of a socket.

    A method with no rpc is absent, and `NO_RPC` says why. A proxy,
    parameter or return, is spelled as its protocol, here and on both
    implementations."""
    spell = Spelling(lambda t: (model.classes[t.name].protocol_name,
                                None))
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
        "not edit. One per served class, so a function typed against "
        "StoreLike accepts an in-process "
        "AsyncStore and a remote RPCStore alike."))))
    mod.body.append(_future_annotations())
    mod.body.append(import_from("typing", "Protocol", "runtime_checkable"))
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
        withheld = sorted(
            (m.name, "; ".join(ir.blockers(m.params, m.returns, model.served)))
            for m in model_cls.methods if not model.offered(m))
        cls.body.append(ast.Expr(value=ast.Constant(value=(
            f"What every {name} implementation promises."
            + ("\n\n    Not promised, because the RPC client cannot offer "
               "them:\n\n" + "".join(f"    - {n}: {why}\n"
                                     for n, why in withheld)
               + "    " if withheld else "")))))
        for m in model_cls.methods:
            if not model.offered(m):
                continue
            params, returns = signatures[(name, m.name)]
            cls.body.append(_def(
                f"async def {m.name}() -> {returns}", doc=m.doc,
                signature=_arguments([ast.arg(arg="self")], m.params, params,
                                     f"{name}.{m.name}")))
        cls.body.append(_def(
            f"async def {ACLOSE}(self) -> None",
            doc="Release this object. In process that shuts the runner's "
                "thread down; remotely it gives the lease back. Either way "
                "the object is spent afterwards."))
        mod.body.append(cls)

    ast.fix_missing_locations(mod)
    return mod


def _spec(name: str, rpc: ir.RpcNames, params: Sequence[ir.ParamModel],
          returns: ir.TypeRef | None) -> cs.Call:
    """One call's spec.

    A typed value, not a dict literal. A checker sees nothing in a
    dict, and the spec is the one part of the generated client a
    caller cannot read, so it must be a part the checker verifies.

    The docstring is dropped: it is already on the method. So are the
    parameter defaults - the method signature resolved them before the
    call reached the runtime, so every argument a spec describes is
    present, and carrying a default here would suggest the runtime
    fills one in."""
    return cs.Call(name, rpc.path, rpc.req, rpc.resp,
                   _args([(p.name, p.type) for p in params]), _wire(returns))


def _directory(model: ir.Model) -> list[ast.stmt]:
    """The three tables a caller reaches BY NAME.

    `NixClient.acquire("Store", "auto")` and
    `NixClient.call_function("gc_stats")` take a string, so neither
    can be a generated method - the name is the argument. The lookup
    is the API, so it is a table, emitted as Python a checker reads.

    Not every class is here. One that crosses as a VALUE has no handle
    to construct into - a caller builds it locally and passes it as an
    argument - and a class only a call hands back is never built
    remotely."""
    acquires = []
    for c in model.acquirable:
        acquires.append((c.name, cs.Acquire(
            c.name, c.acquire.path, c.acquire.req,
            _args([(p.name, p.type) for p in c.ctor]),
            sum(1 for p in c.ctor if p.default is None),
            tuple(p.name for p in c.ctor if p.default == "None"))))
    functions = [model.functions[n] for n in sorted(model.functions)]
    free = [(fn.name, _spec(fn.name, fn.rpc, fn.params, fn.returns))
            for fn in functions if not model.function_blockers(fn)]
    # ...and the ones the wire cannot carry, with the reason.
    #
    # A separate table rather than absence, because the two answers
    # differ and a caller can act on the difference: a name nobody
    # declared is a typo, and a declared function with no RPC surface
    # is a policy the build decided and printed.
    blocked = [(fn.name, "; ".join(model.function_blockers(fn)))
               for fn in functions if model.function_blockers(fn)]
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

    Generated classes, not a `__getattr__` proxy. A proxy resolves a
    method name at call time, so a typechecker sees nothing: it can
    neither reject a call that does not exist nor check the arguments
    of one that does. A generated class has real methods with real
    signatures, so both directions are checked - and the conformance gate compares them against the
    protocol and the in-process wrapper.

    Unlike the in-process side, NO class here is abstract. Locally an
    abstract base has no implementation to construct; remotely every
    handle addresses a real object on the server, and the base is a
    perfectly good view of it - which is the common case, since a
    caller usually does not care which store answered."""
    ordered = model.ordered_served
    # A returned proxy is an RPC class: the server leased a handle. A
    # proxy PARAMETER is spelled as the protocol, as on every surface,
    # and the client refuses an in-process object when it encodes one.
    spell = Spelling(lambda t: (model.classes[t.name].protocol_name,
                                PROTOCOLS))
    returned = Spelling(lambda t: (model.classes[t.name].rpc_name,
                                   None))
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
        "spec the build wrote for it, so a call needs no lookup and names "
        "nothing the build did not put there."))))
    mod.body.append(_future_annotations())
    mod.body.append(import_from("typing", "Any", "Protocol", "cast"))
    # The call specs, from where the build wrote them. Emitted in
    # `_policy` rather than here, because the SERVER reads the same
    # ones - and two derivations of one call's shape is exactly the
    # disagreement this repo generates code to prevent.
    specs = [_spec_name(name, m) for name, m in signatures]
    mod.body.append(import_from("_callspec", "Call", level=1))
    if specs:
        mod.body.append(import_from("_policy", *specs, level=1))
    mod.body.extend(spell.imports())

    # What these classes need from whatever is driving them. Declaring
    # it as a Protocol keeps the dependency pointing the right way: the
    # generated package describes what it requires, and the hand-written
    # client satisfies it without either importing the other.
    #
    # handle_id is Optional because release() blanks it. Passing a
    # blanked one is a real mistake, and the client answers it with a
    # message instead of a protobuf failure.
    mod.body.append(_code("""
        class $client(Protocol):
            $doc

            async def invoke(self, spec: Call, handle_id: str | None,
                             args: list[Any]) -> Any: ...

            async def release(self, obj: Any) -> None: ...
        """, client=CLIENT_PROTOCOL, doc=repr(
            "What an RPC class needs from its client. The client owns the "
            "connection, the codec and the handle lifetime; these classes "
            "own the surface.")))

    for served_cls in ordered:
        name = served_cls.name
        cls = _code("""
            class $rpc:
                $doc
                _wire = $wire
                _client: $client
                handle_id: str | None

                def __init__(self, client: $client, handle_id: str) -> None:
                    self._client = client
                    self.handle_id = handle_id
            """, rpc=served_cls.rpc_name, wire=repr(served_cls.wire),
            client=CLIENT_PROTOCOL, doc=repr(
                f"A {name} living behind a handle on a server. Same surface "
                f"as {served_cls.async_name}, different location."))
        assert isinstance(cls, ast.ClassDef)
        # Only the methods that HAVE an rpc. A method the wire cannot
        # carry keeps its in-process wrapper and is simply absent here;
        # `NO_RPC` says why, and the protocol drops it too.
        for m in served_cls.methods:
            if not model.offered(m):
                continue
            params, returns = signatures[(name, m.name)]
            call = (f"self._client.invoke({_spec_name(name, m.name)}, "
                    f"self.handle_id, [{', '.join(p.name for p in m.params)}])")
            cls.body.append(_def(
                f"async def {m.name}() -> {returns}", _forwarded(call, returns),
                m.doc, _arguments([ast.arg(arg="self")], m.params, params,
                                  f"{name}.{m.name}")))
        cls.body.append(_code("""
            async def $aclose(self) -> None:
                $doc
                await self._client.release(self)
            """, aclose=ACLOSE, doc=repr(
                "Give the lease back. The in-process wrapper shuts its runner "
                "down here; there is no thread to shut down on this side, so "
                "the server's copy is what gets released.")))
        mod.body.append(cls)

    # Annotated: the inferred value type is the join of every class in
    # it, which collapses to type[object] - and object takes no
    # constructor arguments, so a caller could not build one.
    mod.body.append(_code("$registry: dict[str, type[Any]] = {$items}",
                          registry=REGISTRY, items=", ".join(
                              f"{c.name!r}: {c.rpc_name}" for c in ordered)))
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
        mod.body.append(import_from("typing", "cast"))
    mod.body.extend(spell.imports())
    mod.body.append(ast.ImportFrom(
        module="huggorm_bindings",
        names=[ast.alias(name=f.name, asname="_" + f.name) for f in fns],
        level=0))
    runtime_names = ["call_function"] + (["PoolRunner"] if pool_parent else [])
    mod.body.append(import_from("_runtime", *runtime_names, level=1))

    for fn in fns:
        params, ret = signatures[fn.name]
        call = f"call_function(_{fn.name}, [{', '.join(p.name for p in fn.params)}])"
        r = fn.returns
        if r is not None and r.kind == "proxy":
            # A pool policy ignores the parent, so a fresh PoolRunner
            # stands in for the producer a method would pass.
            body = _adopting(call, f"{ASYNC}{r.name}", "PoolRunner(None)",
                             r.optional)
        else:
            body = _forwarded(call, r.spelling if r is not None else "None")
        mod.body.append(_def(f"async def {fn.name}() -> {ret}", body, fn.doc,
                             _arguments([], fn.params, params, fn.name)))

    ast.fix_missing_locations(mod)
    return mod




STUB_PACKAGE = "huggorm_bindings-stubs"

# What the generated RPC classes require of whatever drives them.
CLIENT_PROTOCOL = "RPCClient"


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


def _stub_dunders(name: str, dunders: list[str]) -> list[ast.stmt]:
    """The value dunders a class defines, as stub declarations.

    Everything else in a stub comes from the model's methods. Those are
    surface only, so they skip the value dunders - right for the rpc
    and protocol surfaces, and wrong here. Without these, a typechecker reads object's __eq__ and
    calls `a < b` an error on a class that supports it, and
    `sorted(paths)` an error on a list of them (huggorm#46).

    Which ones exist is reflected, not assumed: ordering and __str__
    are per-class, because a store path has a natural order and a
    natural string while a PathInfo has neither."""
    out: list[ast.stmt] = []
    for dunder in dunders:
        param, ret = _DUNDER_SIGS[dunder]
        other = f", other: {param}" if param is not None else ""
        out.append(_def(f"def {dunder}(self{other}) -> {ret}"))
    return out


def _stub_class(c: ir.ClassModel, spell: Spelling,
                produced: bool) -> ast.ClassDef:
    name = c.name
    cls = ast.ClassDef(name=name, bases=[], keywords=[], body=[],
                       decorator_list=[], type_params=[])
    cls.body.append(ast.Expr(value=ast.Constant(value=(
        c.doc or f"Binding for the C++ {c.binds}. Threading "
                 f"'{c.decl.threading}', wire '{c.wire}'."))))
    # The declarations the codegen itself reads. They are real class
    # attributes, so a stub that omitted them would make every
    # reader of them an error.
    for attr in ("_threading", "_wire", "_binds"):
        cls.body.append(_code(f"{attr}: str"))
    if produced:
        cls.body.append(_def(
            "def __init__(self) -> NoReturn",
            doc=f"Always raises: a {name} is produced by another object, "
                f"never constructed."))
    else:
        spell.defaults(c.ctor)
        cls.body.append(_def(
            "def __init__() -> None",
            signature=_arguments([ast.arg(arg="self")], c.ctor,
                                 [spell(p.type) for p in c.ctor],
                                 f"{name}.__init__")))
    cls.body.extend(_stub_dunders(name, ir.dunders(c.decl)))
    for m in c.methods:
        spell.defaults(m.params)
        params = [spell(p.type) for p in m.params]
        doc = m.doc
        if m.blocks and not c.wrapped:
            doc = (f"{doc}\n\n" if doc else "") + (
                f"Blocks, and {name} has no async form: from async code, "
                f"call it through `anyio.to_thread.run_sync` (huggorm#25).")
        cls.body.append(_def(
            f"def {m.name}() -> {spell.returns(m.returns)}", doc=doc,
            signature=_arguments([ast.arg(arg="self")], m.params, params,
                                 f"{name}.{m.name}")))
    if len(cls.body) == 1:
        # Docstring only: a class body needs a statement.
        cls.body.append(ast.Expr(value=ast.Constant(value=Ellipsis)))
    return cls


def _stub_function(fn: ir.FunctionModel, spell: Spelling,
                   where: str) -> ast.stmt:
    spell.defaults(fn.params)
    params = [spell(p.type) for p in fn.params]
    return _def(f"def {fn.name}() -> {spell.returns(fn.returns)}", doc=fn.doc,
                signature=_arguments([], fn.params, params, where))


def _homes(model: ir.Model) -> dict[str, str]:
    """The binding module each declared name is defined in."""
    home = {c.name: c.qualified_module for c in model.classes.values()}
    home.update({n: e.module for n, e in model.enums.items()})
    if model.errors.module:
        home.update({n: model.errors.module for n in model.errors.classes})
    return home


def stub_module(model: ir.Model, module: str) -> ast.Module:
    """Emit the .pyi describing ONE binding module.

    The bindings ship as compiled extensions. A typechecker cannot read
    a .so, so without this every binding type is Any - which is why a
    protocol-typed consumer could catch a call to a method that does
    not exist and NOT catch a str passed where a StorePath is declared
    (huggorm#27).

    A class only a call hands back raises from __init__, so the stub
    says NoReturn - true, and it makes StorePath() an error at the call
    site instead of a TypeError at runtime.

    A union is written out as its arms. The alias is Python in the
    generated `_unions` module, and a compiled binding module holds no
    such name, so a stub that NAMED it would name nothing."""
    spell = Spelling(_as_binding, expand=model.unions)
    classes = [c for c in (*model.handed_back, *model.constructed)
               if c.qualified_module == module]
    functions = [model.functions[n] for n in sorted(model.functions)
                 if model.functions[n].module == module]
    produced = {c.name for c in classes
                if c.name in model.returned or c.produced}
    short = module.rsplit(".", 1)[-1]
    defs: list[ast.stmt] = [_stub_class(c, spell, c.name in produced)
                            for c in classes]
    defs += [_stub_function(fn, spell, f"{short}.{fn.name}")
             for fn in functions]

    mod = ast.Module(body=[], type_ignores=[])
    mod.body.append(ast.Expr(value=ast.Constant(value=(
        f"Generated type stubs for {module} - do not edit.\n\n"
        f"The module itself is a compiled extension, which carries no "
        f"signatures a typechecker can read. Built via ast at Nix build "
        f"time, from the same model as every other surface."))))
    if produced:
        mod.body.append(ast.ImportFrom(
            module="typing", names=[ast.alias(name="NoReturn")], level=0))
    mod.body.extend(spell.module_imports())
    home = _homes(model)
    for name in sorted(spell.bindings):
        if home.get(name, module) != module:
            mod.body.append(ast.ImportFrom(
                module=home[name].rsplit(".", 1)[-1],
                names=[ast.alias(name=name)], level=1))
    mod.body.extend(defs)
    ast.fix_missing_locations(mod)
    return mod


def stub_package(model: ir.Model) -> dict[str, ast.Module]:
    """Every binding module's stub, and the `__init__.pyi` that
    re-exports them, by file name.

    The vocabularies get NO .pyi of their own and want none: they are
    plain Python, and `partial` in py.typed is exactly the instruction
    to read the real module for anything these stubs do not cover.
    Only __init__.pyi mentions them, because it must re-export what
    the package does."""
    modules = sorted({c.qualified_module for c in model.classes.values()}
                     | {f.module for f in model.functions.values()})
    out: dict[str, ast.Module] = {}
    exported: dict[str, list[str]] = {}
    for module in modules:
        out[f"{module.rsplit('.', 1)[-1]}.pyi"] = stub_module(model, module)
        exported[module] = (
            [c.name for c in (*model.handed_back, *model.constructed)
             if c.qualified_module == module]
            + [n for n in sorted(model.functions)
               if model.functions[n].module == module])
    for name, enum in sorted(model.enums.items()):
        exported.setdefault(enum.module, []).append(name)
    out["__init__.pyi"] = stub_init_module(
        {m: exported[m] for m in sorted(exported)})
    return out


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


def init_module(model: ir.Model) -> ast.Module:
    """The package front door: every served class in its three forms -
    the protocol it promises, the in-process implementation and the RPC
    implementation - plus the free functions."""
    served = model.ordered_served
    free_names = wrapped_functions(model)
    mod = ast.Module(body=[], type_ignores=[])
    mod.body.append(
        ast.Expr(
            value=ast.Constant(
                value="Generated surface - do not edit. Built via ast at Nix build time."
            )
        )
    )
    for c in served:
        mod.body.append(import_from(f"async_{c.name.lower()}", c.async_name, level=1))
    mod.body.append(import_from(PROTOCOL_MODULE, *(c.protocol_name for c in served),
                                level=1))
    mod.body.append(import_from(RPC_MODULE, REGISTRY, *(c.rpc_name for c in served),
                                level=1))
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
                      for n in package_exports(model)]),
        )
    )
    ast.fix_missing_locations(mod)
    return mod


def wrapped_functions(model: ir.Model) -> list[str]:
    """Every free function with an async form, by name."""
    return [n for n in sorted(model.functions) if model.functions[n].wrapped]


def package_exports(model: ir.Model) -> list[str]:
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
    return ([n for c in model.ordered_served
             for n in (c.async_name, c.protocol_name, c.rpc_name)]
            + [REGISTRY] + wrapped_functions(model))
