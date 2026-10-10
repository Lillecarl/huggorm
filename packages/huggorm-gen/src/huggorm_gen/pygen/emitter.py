"""Emit ast trees from the typed model (`huggorm_gen.ir`).

Pure tree building - no I/O. Everything an emitter needs arrives in
the model.
"""

import ast
import dataclasses
import enum
import textwrap
from collections.abc import Mapping, Sequence
from string import Template
from typing import assert_never

from huggorm_dsl.declare import Crossing, Threading
from huggorm_gen import ir
from huggorm_gen.payload import _callspec as cs
from huggorm_gen.payload._wiretypes import (
    python_spelling,
)
from huggorm_gen.pygen.spell import (
    BINDINGS,
    PROTOCOLS,
    Rename,
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


def _twinned(call: str, t: ir.TypeRef) -> str:
    """The body that hands back a forward in its async spelling.

    anyio.Path takes any path-like, so the wrapper constructs one
    rather than casting: a cast would claim the awaitable methods
    without adding them."""
    twin = t.leaf.twin
    if not t.origin:
        return f"return {twin}(await {call})"
    if t.inner.origin:
        raise TypeError(f"{t.spelling}: an async twin is spelled under one "
                        f"`| None` or `list[...]`, and this nests deeper")
    match t.origin:
        case ir.Origin.OPTIONAL:
            return (f"result = await {call}\n"
                    f"return None if result is None else {twin}(result)")
        case ir.Origin.LIST:
            return f"return [{twin}(item) for item in await {call}]"
        case ir.Origin.DICT:
            raise TypeError(f"{t.spelling}: an async twin has no spelling "
                            f"inside a dict")
        case _:
            assert_never(t.origin)


def _adapted(t: ir.TypeRef) -> bool:
    """A `@calls_back` argument, which may be an async object."""
    return not t.container and t.leaf.kind == ir.Kind.CLIENT


def _passed(params: Sequence[ir.ParamModel]) -> str:
    """The arguments a call hands its runner. A `@calls_back` one goes
    through `adapt`, which wraps an async object in a stub Nix can
    call (huggorm#155)."""
    return ", ".join(f"adapt({p.type.leaf.name}, {p.name})"
                     if _adapted(p.type) else p.name for p in params)


def _client_rename(source: Source | None) -> Rename:
    """A `@calls_back` class's async protocol, from `source`."""
    return lambda t: (f"{ASYNC}{t.name}", source)


RUNNER = {
    ir.Execution.AFFINE: "AffineRunner",
    ir.Execution.POOL: "PoolRunner",
    ir.Execution.INLINE: "InlineRunner",
}

def _arguments(leading: list[ast.arg], params: Sequence[ir.ParamModel],
               types: list[str], where: str) -> ast.arguments:
    """`leading` plus one argument per declared parameter, annotated
    and defaulted.

    `types` is the annotation each parameter is written with. The
    caller resolves it, because the same declared type is spelled
    differently in an async class, in a protocol and in a stub.

    The default is written from the declared source string, so every
    surface offers the same one. A caller that omits the argument gets
    the same value in-process and over RPC, and the wire never has to
    represent absence."""
    args = list(leading)
    defaults: list[ast.expr] = []
    for p, type_str in zip(params, types, strict=True):
        annotation = _ann(type_str, f"{where}:{p.name}")
        if p.defaults_to_none and not _admits_none(annotation):
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
# The module the `@calls_back` protocols are in.
PROTOCOL_MODULE = "protocols"

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
    `Wire(WireKind.LIST, item=...)` leaves `name` out, and the emitted
    call is the one a person would write.

    An enum member is written as its attribute, and is tested before
    `str`: a StrEnum member IS a str, and as a constant it would lose
    its class."""
    if isinstance(v, enum.Enum):
        return ast.Attribute(value=ast.Name(id=type(v).__name__), attr=v.name)
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


# The kinds that cross by name. A scalar crosses by its builtin and a
# container by its item, so neither is here.
_CROSSES = {
    ir.Kind.ENUM: cs.WireKind.ENUM,
    ir.Kind.VALUE: cs.WireKind.VALUE,
    ir.Kind.UNION: cs.WireKind.UNION,
    ir.Kind.ERROR: cs.WireKind.ERROR,
    ir.Kind.PROXY: cs.WireKind.PROXY,
    ir.Kind.CLIENT: cs.WireKind.CLIENT,
}


def _wire(t: ir.TypeRef | None) -> cs.Wire | None:
    """A resolved type as the `Wire` the codec dispatches on."""
    if t is None:
        return None
    optional = t.optional
    t = t.required
    match t.origin:
        case ir.Origin.LIST:
            return cs.Wire(cs.WireKind.LIST, item=_wire(t.args[0]),
                           optional=optional)
        case ir.Origin.DICT:
            return cs.Wire(cs.WireKind.MAP, item=_wire(t.args[0]),
                           optional=optional)
        case ir.Origin.OPTIONAL:
            raise TypeError(f"{t.spelling}: `| None` under `| None`")
        case None:
            pass
        case _:
            assert_never(t.origin)
    if t.scalar is not None:
        # The leaf's own name, not the builtin it goes in as: the codec
        # converts a `datetime.timedelta` by name.
        return cs.Wire(cs.WireKind.SCALAR, t.width or t.name,
                       optional=optional)
    if (kind := _CROSSES.get(t.kind)) is not None:
        return cs.Wire(kind, t.name, optional=optional)
    raise TypeError(f"{t.spelling} is a {t.kind}, which does not cross")


def _args(pairs: Sequence[tuple[str, ir.TypeRef]]) -> tuple[cs.Arg, ...]:
    """Named, typed parts - wire fields or parameters - as `Arg`s."""
    out = []
    for name, t in pairs:
        w = _wire(t)
        assert w is not None
        out.append(cs.Arg(name, w))
    return tuple(out)


class _Specs:
    """The call spec constants, numbered as they are written.

    `CALLS` lists them in that order, so `CALLS[n].index == n`."""

    def __init__(self, body: list[ast.stmt]) -> None:
        self._body = body
        self.names: list[ast.Name] = []

    @property
    def next(self) -> int:
        return len(self.names)

    def add(self, var: str, spec: cs.Call | cs.Acquire) -> ast.Name:
        assert spec.index == self.next, (var, spec.index, self.next)
        self._body.append(ast.Assign(targets=[ast.Name(id=var)],
                                     value=_literal(spec)))
        self.names.append(ast.Name(id=var))
        return ast.Name(id=var)


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
        import_from("_callspec", "Acquire", "Arg", "Builds", "Call", "Entries",
                    "Hook", "Items", "Leaf", "Local", "Null", "Subscription",
                    "Tree", "Wire", "WireKind", level=1),
    ]
    body.append(_table("WIRE_FIELDS", "dict[str, tuple[Arg, ...]]",
                       [(c.name, _args([(f.name, f.type) for f in c.wire_fields]))
                        for c in classes if c.wire is Crossing.VALUE]))
    # The exception surface. ERROR_MODULE is where the emitted module
    # lands, which the fault codec imports to construct one; the
    # fields are what it is rebuilt FROM.
    body.append(ast.AnnAssign(
        target=ast.Name(id="ERROR_MODULE"), annotation=_ann("str", "ERROR_MODULE"),
        value=ast.Constant(value=model.errors.module), simple=1))
    body.append(_table("ERROR_FIELDS", "dict[str, tuple[Arg, ...]]", [
        (n, _args([(f.name, f.type) for f in e.wire_fields]))
        for n, e in model.errors.classes.items()]))
    body.append(_table("UNION_ARMS", "dict[str, tuple[Wire, ...]]", [
        (n, tuple(_wire(a) for a in u.arms))
        for n, u in model.unions.items()]))
    # Every call's spec, ONCE. The async classes hand them to a runner
    # or to the client, and the server reads the tables, so no two of
    # them can disagree about a call's shape.
    specs = _Specs(body)
    methods = []
    for c in model.ordered_served:
        offered: list[ast.expr] = []
        for m in c.methods:
            if model.offered(m):
                offered.append(specs.add(
                    _spec_name(c.name, m.name),
                    _spec(specs.next, m.name, m.params, m.returns,
                          m.subscription)))
            else:
                body.append(ast.Assign(
                    targets=[ast.Name(id=_spec_name(c.name, m.name))],
                    value=_literal(cs.Local(m.name, _wire(m.returns), "; ".join(
                        ir.blockers(m.params, m.returns, model.served))))))
        methods.append((c.name, ast.Tuple(elts=offered)))
    body.append(_table("METHODS", "dict[str, tuple[Call, ...]]", methods))
    # What the server may call on a client's object: each `@virtual`,
    # numbered within its class (huggorm#153).
    body.append(_table("CALLBACKS", "dict[str, tuple[Hook, ...]]", [
        (c.name, tuple(cs.Hook(_spec(i, m.name, m.params, m.returns), m.posted)
                       for i, m in enumerate(c.hooks)))
        for c in model.called_back]))
    # The value TREES, and how values are built.
    body.append(_table("TREES", "dict[str, Tree]", [
        (c.name, c.tree) for c in classes if c.tree is not None]))
    body.append(_table("BUILDERS", "dict[str, Builds]", [
        (c.name, c.builds) for c in classes if c.builds is not None]))
    body.extend(_directory(model, specs))
    body.append(ast.AnnAssign(
        target=ast.Name(id="CALLS"),
        annotation=_ann("tuple[Call | Acquire, ...]", "CALLS"),
        value=ast.Tuple(elts=list(specs.names)), simple=1))
    return ast.unparse(ast.fix_missing_locations(
        ast.Module(body=body, type_ignores=[]))) + "\n"


def unions_module(unions: Mapping[str, ir.UnionModel]) -> str:
    """`_unions.py`: one alias per declared sum type.

    Nothing but aliases, and every one derived from the model - so
    the declaration says `DerivedPath = StorePath | DerivedPathBuilt`
    once and this is the same sentence in the package a caller
    imports."""
    # A scalar arm is a builtin, and huggorm_bindings has none to import.
    arms = sorted({a.name for u in unions.values() for a in u.arms
                   if a.kind != ir.Kind.SCALAR})
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
    for alias, union in unions.items():
        members = union.arms
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




def _as_binding(t: ir.TypeRef) -> tuple[str, Source | None]:
    return t.name, BINDINGS


def _as_async(t: ir.TypeRef) -> tuple[str, Source | None]:
    return f"{ASYNC}{t.name}", sibling(t.name)


def _widened(spell: Spelling, model: ir.Model, t: ir.TypeRef,
             client: bool = False) -> str:
    """A constructor's or a free function's parameter: a bare proxy
    takes the sync object or its async wrapper.
    `client` widens a `@calls_back` class where the call adapts it."""
    if t.kind == ir.Kind.PROXY and not t.origin and t.name in model.served:
        spell.need(t.name, BINDINGS)
        spell.need(f"{ASYNC}{t.name}", sibling(t.name))
        return f"{t.name} | {ASYNC}{t.name}"
    return spell(t, _as_binding, client=client)


def _async_spelling(model: ir.Model, c: ir.ClassModel
                    ) -> tuple[Spelling, list[str],
                               dict[str, tuple[list[str], str]]]:
    """How the in-process async module for `c` spells every type it
    writes: the constructor's parameters, then each method's parameters
    and return.

    A constructor takes the sync object or its async wrapper, and only
    a bare proxy is widened so. A served class, parameter or adopted
    return, is its async class. A type with an async twin is the twin,
    and everything else is itself. An unserved class has no async
    class, so a method that takes one takes the binding class: the
    method has no RPC, and the runner calls it in this process."""
    spell = Spelling(lambda t: _as_async(t) if t.name in model.served
                     else _as_binding(t),
                     client=_client_rename(PROTOCOLS))
    ctor = [_widened(spell, model, p.type) for p in c.ctor]
    methods: dict[str, tuple[list[str], str]] = {}
    for m in c.methods:
        params = [spell(p.type, client=True) for p in m.params]
        if model.adopted(m.returns) is not None:
            ret = spell.returns(m.returns, _as_async)
        elif m.returns is not None:
            ret = spell(m.returns, _as_binding, twin=True)
        else:
            ret = "None"
        methods[m.name] = (params, ret)
        spell.defaults(m.params)
    return spell, ctor, methods


def _hop_method(cls: ast.ClassDef, model: ir.Model,
                m: ir.MethodModel, svc: str,
                signature: tuple[list[str], str]) -> None:
    """One `async def` that sends its spec to the backend. The backend
    hands a returned served class back in its async form."""
    params, returns = signature
    call = (f"self._backend.call({_spec_name(svc, m.name)}, "
            f"[{', '.join(p.name for p in m.params)}])")
    if m.returns is not None and m.returns.leaf.twin:
        body = _twinned(call, m.returns)
    else:
        body = _forwarded(call, returns)
    cls.body.append(_def(
        f"async def {m.name}() -> {returns}", body, m.doc,
        _arguments([ast.arg(arg="self")], m.params, params, f"{svc}.{m.name}")))


_POLICY_DOC = {
    ir.Execution.AFFINE: "operations run on the producer's thread.",
    ir.Execution.POOL: "operations may run on any pool thread.",
    ir.Execution.INLINE: "operations run on the calling thread, because none of "
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
        _code("_copied = $copied", copied=repr(c.copied)),
        _code("_backend: Backend"),
        init,
        _code("""
            @classmethod
            def _on(cls, backend: Backend) -> Self:
                obj = cls.__new__(cls)
                obj._backend = backend
                return obj
            """),
        _code("""
            @classmethod
            def _adopt(cls, obj: $svc, runner: BaseRunner) -> Self:
                return cls._on($adopter.adopt(obj, runner))
            """, svc=c.name, adopter=RUNNER[c.execution]),
    ])
    for m in c.methods:
        _hop_method(cls, model, m, c.name, methods[m.name])
    cls.body.append(_code("""
        async def aclose(self) -> None:
            await self._backend.aclose()
        """))
    # A call hands back Any, and cast is where the declared type is
    # claimed. A twin is built instead, and a None return does not
    # return.
    if any(m.returns is not None and not m.returns.leaf.twin
           for m in c.methods):
        typing_names = typing_names | {"cast"}
    # Docstring FIRST: anything before it demotes it to a dead
    # expression and leaves the module with no __doc__.
    mod = ast.Module(type_ignores=[], body=[
        ast.Expr(value=ast.Constant(value=doc)),
        _future_annotations(),
        import_from("typing", "Self", *typing_names),
        import_from("_runtime", "Backend", "BaseRunner",
                    *sorted({RUNNER[c.execution], *runtime_names}), level=1),
        *([import_from("_policy", *(_spec_name(c.name, m.name)
                                    for m in c.methods), level=1)]
          if c.methods else []),
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
    thread, and the runner its own execution names adopts it. A
    returned class can produce another one - a Value holds Values -
    and that return is adopted too."""
    policy = c.threading
    init = _code("""
        def __init__(self, obj: $svc, runner: BaseRunner) -> None:
            self._backend = $adopter.adopt(obj, runner)
        """, svc=c.name, adopter=RUNNER[c.execution])
    return _async_module(
        model, c,
        f"Generated async wrapper for returned type {c.name} "
        f"(threading: {policy}) - do not edit.",
        f"A {c.name} that another call hands back, in this process or on "
        f"a server. Policy '{policy}' in process: " + _POLICY_DOC[c.execution],
        init, set(), set())


def wrapper_module(model: ir.Model, c: ir.ClassModel) -> ast.Module:
    """Async<X> for a class a caller constructs.

    The object is built lazily, on the runner's own thread, from the
    declared constructor's arguments. A class with no door refuses to
    be built and is only ever received from a call."""
    svc = c.name
    threading = c.threading
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
            f"A {svc}, in this process or on a server. Only a call hands one "
            f"back: nothing declared constructs it.",
            init, {"Any"}, set())
    runner = RUNNER[ir.Execution(threading)]
    name = (f", name={f'huggorm-affine-{svc}'!r}"
            if threading is Threading.AFFINE
            else "")
    _, ctor, _ = _async_spelling(model, c)
    # A zero-argument lambda over __init__'s parameters, so the object
    # is built on the runner's thread, not the caller's. Each argument
    # goes through unwrap_arg: a wrapper passed in contributes its
    # target object, not the async shell.
    init = _code("""
        def __init__(self) -> None:
            self._backend = $runner(lambda: $svc($values)$name)
        """, _arguments([ast.arg(arg="self")], c.ctor, ctor, f"{svc}.__init__"),
        runner=runner, svc=svc, name=name,
        values=", ".join(f"unwrap_arg({p.name})" for p in c.ctor))
    # `unwrap_arg` only when the factory calls it: an unused import is
    # what `test_no_unused_imports` rejects.
    return _async_module(
        model, c, doc,
        f"A {svc}, in this process or on a server. Constructed here, it is "
        f"built lazily on its runner thread; `NixClient.acquire` builds one "
        f"on a server.",
        init, set(), {runner} | ({"unwrap_arg"} if c.ctor else set()))


def _future_annotations() -> ast.ImportFrom:
    """Lazy annotations, so a class may name one defined further down.

    An async class's method can name its own class or a protocol, and
    a protocol's hook can name a class defined later. PEP 649 already
    defers evaluation on 3.14; the package also runs on 3.11 and up,
    where an async wrapper's method returning its own class needs this
    (huggorm#107)."""
    return ast.ImportFrom(module="__future__",
                          names=[ast.alias(name="annotations")], level=0)


def protocol_module(model: ir.Model) -> ast.Module:
    """Emit one Protocol per `@calls_back` class: the async object a
    program writes for Nix to call, in process or on a remote client
    (huggorm#155).

    A hook takes and answers what the sync hook does: the stub that
    calls it hands the answer to Nix unchanged."""
    spell = Spelling(_as_async)
    hooks = {
        (cls.name, m.name): ([spell(p.type) for p in m.params],
                             spell.returns(m.returns))
        for cls in model.called_back for m in cls.hooks
    }
    mod = ast.Module(body=[], type_ignores=[])
    mod.body.append(ast.Expr(value=ast.Constant(value=(
        "Generated protocols: the objects a program writes for Nix to "
        "call - do not edit."))))
    mod.body.append(_future_annotations())
    mod.body.append(import_from("typing", "Protocol"))
    mod.body.extend(spell.imports())

    for model_cls in model.called_back:
        cls = ast.ClassDef(
            name=model_cls.async_name, bases=[ast.Name(id="Protocol")],
            keywords=[], decorator_list=[], type_params=[], body=[
                ast.Expr(value=ast.Constant(value=(
                    f"A {model_cls.name} written for the event loop, in "
                    f"process or on a remote client (huggorm#155).\n\n"
                    f"    Nix calls each method from the thread of the call "
                    f"that reads it, and\n    the method runs on the loop, "
                    f"in that call's task. It may await\n    other calls, "
                    f"the same EvalState's included.\n    ")))])
        for m in model_cls.hooks:
            params, returns = hooks[(model_cls.name, m.name)]
            cls.body.append(_def(
                f"async def {m.name}() -> {returns}", doc=m.doc,
                signature=_arguments([ast.arg(arg="self")], m.params, params,
                                     f"{model_cls.async_name}.{m.name}")))
        mod.body.append(cls)

    ast.fix_missing_locations(mod)
    return mod


def _spec(index: int, name: str, params: Sequence[ir.ParamModel],
          returns: ir.TypeRef | None,
          subscription: cs.Subscription | None = None) -> cs.Call:
    """One call's spec.

    A typed value, not a dict literal. A checker sees nothing in a
    dict, and the spec is the one part of the generated client a
    caller cannot read, so it must be a part the checker verifies.

    The docstring is dropped: it is already on the method. So are the
    parameter defaults - the method signature resolved them before the
    call reached the runtime, so every argument a spec describes is
    present, and carrying a default here would suggest the runtime
    fills one in."""
    return cs.Call(index, name, _args([(p.name, p.type) for p in params]),
                   _wire(returns), subscription)


def _directory(model: ir.Model, specs: _Specs) -> list[ast.stmt]:
    """The three tables a caller reaches BY NAME.

    `NixClient.acquire("Store", "auto")` and
    `NixClient.call_function("gc_stats")` take a string, so neither
    can be a generated method - the name is the argument. The lookup
    is the API, so it is a table, emitted as Python a checker reads.

    Not every class is here. One that crosses as a VALUE has no handle
    to construct into - a caller builds it locally and passes it as an
    argument - and a class only a call hands back is never built
    remotely."""
    acquires = [(c.name, specs.add(f"_acquire_{c.name}", cs.Acquire(
        specs.next, c.name, _args([(p.name, p.type) for p in c.ctor]),
        sum(1 for p in c.ctor if p.default is None))))
        for c in model.acquirable]
    functions = [model.functions[n] for n in sorted(model.functions)]
    free = [(fn.name, specs.add(f"_fn_{fn.name}", _spec(
        specs.next, fn.name, fn.params, fn.returns, fn.subscription)))
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

    Module level, not a class attribute: an attribute shadows rather
    than merges, so a subclass that adds a method would restate the
    base's whole table. A constant per method has no such rule.

    Leading underscore, because it is not surface: a caller reads the
    method, not what the build decided the method does."""
    return f"_{cls}_{method}"


CLASSES_MODULE = "_classes"


def classes_module(model: ir.Model) -> ast.Module:
    """`_classes.py`: the classes the runner finds by a spec's declared
    name.

    `CLASSES` holds each served class's async form, which the runner
    adopts a returned proxy into. `CALLED_BACK` holds each `@calls_back`
    binding, which `adapt` subclasses for an argument that is a
    program's own object. A module of its own, because it imports every
    async class, and each of those imports `_policy`."""
    served = model.ordered_served
    hooked = [c.name for c in model.called_back]
    mod = ast.Module(type_ignores=[], body=[
        ast.Expr(value=ast.Constant(value=(
            "Generated: the classes the runner finds by a spec's declared "
            "name - do not edit."))),
        import_from("typing", "Any"),
        *([import_from("huggorm_bindings", *hooked)] if hooked else []),
        *(import_from(f"async_{c.name.lower()}", c.async_name, level=1)
          for c in served),
        _table("CLASSES", "dict[str, type[Any]]",
               [(c.name, ast.Name(id=c.async_name)) for c in served]),
        _table("CALLED_BACK", "dict[str, type[Any]]",
               [(n, ast.Name(id=n)) for n in hooked]),
    ])
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
    return is. `contracts.free_functions` refuses one that cannot be."""
    fns = async_functions(model)
    pool_parent = any(model.adopted(fn.returns) is not None for fn in fns)

    spell = Spelling(client=_client_rename(PROTOCOLS))
    signatures = {}
    for fn in fns:
        if fn.calls is not None:
            spell.need(fn.calls.cls, BINDINGS)
        params = [_widened(spell, model, p.type, client=True)
                  for p in fn.params]
        r = fn.returns
        ret = (spell.returns(r, _as_async)
               if model.adopted(r) is not None
               else spell.returns(r, _as_binding))
        signatures[fn.name] = (params, ret)
        spell.defaults(fn.params)

    mod = ast.Module(body=[], type_ignores=[])
    mod.body.append(ast.Expr(value=ast.Constant(
        value="Generated async wrappers for the bindings' module-level "
              "functions - do not edit. Built via ast at Nix build time.")))
    mod.body.append(_future_annotations())
    mod.body.append(import_from("collections.abc", "Awaitable", "Callable"))
    mod.body.append(import_from("typing", "Any", *(
        ["cast"] if any(f.returns is not None and model.adopted(f.returns) is None
                        for f in fns) else [])))
    mod.body.extend(spell.imports())
    mod.body.append(ast.ImportFrom(
        module="huggorm_bindings",
        names=[ast.alias(name=f.name, asname="_" + f.name)
               for f in fns if f.calls is None],
        level=0))
    runtime_names = (["call_function"] + (["PoolRunner"] if pool_parent else [])
                     + (["adapt"] if any(_adapted(p.type) for fn in fns
                                         for p in fn.params) else []))
    mod.body.append(import_from("_runtime", *runtime_names, level=1))

    for fn in fns:
        params, ret = signatures[fn.name]
        target = ("_" + fn.name if fn.calls is None
                  else f"{fn.calls.cls}.{fn.calls.method}")
        call = f"call_function({target}, [{_passed(fn.params)}])"
        r = fn.returns
        if (adopted := model.adopted(r)) is not None:
            # A pool policy ignores the parent, so a fresh PoolRunner
            # stands in for the producer a method would pass.
            body = _adopting(call, f"{ASYNC}{adopted.name}",
                             "PoolRunner(None)", r is not None and r.optional)
        else:
            body = _forwarded(call, r.spelling if r is not None else "None")
        mod.body.append(_def(f"async def {fn.name}() -> {ret}", body, fn.doc,
                             _arguments([], fn.params, params, fn.name)))

    # The server finds the coroutine a wire call names here, not by an
    # attribute lookup on the package.
    mod.body.append(_table("FUNCTIONS", "dict[str, Callable[..., Awaitable[Any]]]",
                           [(fn.name, ast.Name(id=fn.name)) for fn in fns]))
    ast.fix_missing_locations(mod)
    return mod




STUB_PACKAGE = "huggorm_bindings-stubs"

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
    surface only, so they skip the value dunders - right for the
    async classes, and wrong here. Without these, a typechecker reads
    object's __eq__ and calls `a < b` an error on a class that supports
    it, and `sorted(paths)` an error on a list of them (huggorm#46).

    Which ones exist is reflected, not assumed: ordering and __str__
    are per-class, because a store path has a natural order and a
    natural string while a PathInfo has neither."""
    out: list[ast.stmt] = []
    for dunder in dunders:
        param, ret = _DUNDER_SIGS[dunder]
        other = f", other: {param}" if param is not None else ""
        out.append(_def(f"def {dunder}(self{other}) -> {ret}"))
    return out


def _stub_class(c: ir.ClassModel, spell: Spelling, produced: bool,
                coroutines: Mapping[ir.MethodRef | None, str]) -> ast.ClassDef:
    name = c.name
    bases: list[ast.expr] = []
    if c.base:
        spell.need(c.base, BINDINGS)
        bases.append(ast.Name(id=c.base))
    cls = ast.ClassDef(name=name, bases=bases, keywords=[], body=[],
                       decorator_list=[], type_params=[])
    cls.body.append(ast.Expr(value=ast.Constant(value=(
        c.doc or f"Binding for the C++ {c.binds}. Threading "
                 f"'{c.threading}', wire '{c.wire}'."))))
    # The one class attribute the runtime reads off a binding.
    cls.body.append(_code("_copied: bool"))
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
    cls.body.extend(_stub_dunders(name, c.dunders))
    for m in c.methods:
        spell.defaults(m.params)
        params = [spell(p.type) for p in m.params]
        doc = m.doc
        if (coroutine := coroutines.get(ir.MethodRef(name, m.name))) is not None:
            doc = (f"{doc}\n\n" if doc else "") + (
                f"Blocks. From async code, await `huggorm.{coroutine}` "
                f"(huggorm#25).")
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
    a .so, so without this every binding type is Any - which is why an
    async-typed consumer could catch a call to a method that does
    not exist and NOT catch a str passed where a StorePath is declared
    (huggorm#27).

    A class only a call hands back raises from __init__, so the stub
    says NoReturn - true, and it makes StorePath() an error at the call
    site instead of a TypeError at runtime.

    A union is written out as its arms. The alias is Python in the
    generated `_unions` module, and a compiled binding module holds no
    such name, so a stub that NAMED it would name nothing."""
    spell = Spelling(_as_binding, expand={n: u.arms for n, u
                                          in model.unions.items()})
    classes = [c for c in (*model.handed_back, *model.constructed)
               if c.qualified_module == module]
    functions = [model.functions[n] for n in sorted(model.functions)
                 if model.functions[n].module == module]
    produced = {c.name for c in classes
                if c.name in model.returned or c.produced}
    short = module.rsplit(".", 1)[-1]
    coroutines = {f.calls: f.name for f in model.blocking_methods}
    defs: list[ast.stmt] = [_stub_class(c, spell, c.name in produced,
                                        coroutines)
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
    for module in modules:
        out[f"{module.rsplit('.', 1)[-1]}.pyi"] = stub_module(model, module)
    out["__init__.pyi"] = stub_init_module(model.exports)
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
            module=module,
            names=[ast.alias(name=n, asname=n) for n in exported], level=1))
        names += exported
    mod.body.append(ast.Assign(
        targets=[ast.Name(id="__all__")],
        value=ast.List(elts=[ast.Constant(value=n) for n in names])))
    ast.fix_missing_locations(mod)
    return mod


def init_module(model: ir.Model) -> ast.Module:
    """The package front door: every served class, every `@calls_back`
    protocol, and the free functions."""
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
    if model.called_back:
        mod.body.append(import_from(
            PROTOCOL_MODULE, *(c.async_name for c in model.called_back), level=1))
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


def async_functions(model: ir.Model) -> list[ir.FunctionModel]:
    """Every module-level coroutine, by name: each free function with an
    async form, and the async form of each method a value blocks in."""
    return sorted([*(f for f in model.functions.values() if f.wrapped),
                   *model.blocking_methods], key=lambda f: f.name)


def wrapped_functions(model: ir.Model) -> list[str]:
    """Every module-level coroutine's name."""
    return [f.name for f in async_functions(model)]


def package_exports(model: ir.Model) -> list[str]:
    """Everything `huggorm_generated` offers, in `__all__` order.

    Apart from `init_module` because a second file needs the same
    list: `huggorm.__init__` re-exports this package whole, and the
    front door is emitted too (huggorm#64). Computing it there as well
    would be one list stated twice, which is exactly the thing that
    front door existed as."""
    return ([c.async_name for c in model.ordered_served]
            + [c.async_name for c in model.called_back]
            + wrapped_functions(model))
