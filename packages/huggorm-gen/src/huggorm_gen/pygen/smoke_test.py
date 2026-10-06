"""
Verify the freshly generated package. Installed as the `codegen-smoke`
entry point; runs after codegen-generate, stdlib only:

1. every emitted .py parses
2. the package imports and __all__ matches
3. every emitted module and class carries a real docstring, and
   imports exactly the names it uses
4. each wrapper constructor is the declared one, and wire policy and
   wrapping hold as the model states them
5. the emitter-runtime symbol contract: every name any emitted module
   imports from _runtime must exist on the runtime module
"""

import argparse
import ast
import builtins
import importlib
import inspect
import pathlib
import re
import sys
from typing import Any

from huggorm_dsl.declare import Crossing, Threading
from huggorm_dsl.read import is_surface


def test_parse(out: pathlib.Path) -> None:
    for py in sorted(out.glob("*.py")):
        ast.parse(py.read_text(), filename=str(py))


def _cls(name: str, *, threading: Threading = Threading.POOL,
         blocking: bool = True,
         wire: Crossing = Crossing.PROXY, fields: tuple[Any, ...] = (),
         returns: tuple[tuple[str, Any], ...] = ()) -> Any:
    """One class model, built by hand for a contract the corpus does
    not break."""
    from huggorm_gen import ir

    return ir.ClassModel(
        name=name, package="pkg", module="mod", doc="",
        wire=wire, threading=threading, blocking=blocking,
        is_value=wire is Crossing.VALUE, produced=False, constructs=True,
        wire_fields=fields, ctor=(),
        bound=tuple(ir.MethodModel(m, (), t, "") for m, t in returns))


def _model(*classes: Any, enums: tuple[str, ...] = ()) -> Any:
    from huggorm_gen import ir

    return ir.Model({c.name: c for c in classes}, {}, {},
                    {n: ir.EnumModel(n, "pkg.mod", (), "")
                     for n in enums},
                    ir.Errors("", {}))


def test_a_container_of_wrapped_types_is_refused() -> None:
    """A container of wrapped types builds, emits a schema, and then
    hands back bare sync objects: nothing attaches a runner to
    elements."""
    from huggorm_gen import contracts, ir

    v = ir.TypeRef.named("V", ir.Kind.PROXY)
    model = _model(_cls("V", returns=(
        ("attrs", ir.TypeRef.dict_of(v)), ("items", ir.TypeRef.list_of(v)),
        ("one", v), ("maybe", ir.TypeRef.optional_of(v)),
        ("n", ir.TypeRef.named("int", ir.Kind.SCALAR)))))
    bad = contracts.collection(model)
    assert len(bad) == 2, bad
    assert all("attrs" in b or "items" in b for b in bad), bad


def test_an_unwrapped_class_hands_back_nothing_wrapped() -> None:
    """A pool class that cannot block is not wrapped, so a caller holds
    its sync object - and a wrapped return would arrive with no
    runner."""
    from huggorm_gen import contracts, ir

    w = ir.TypeRef.named("W", ir.Kind.PROXY)
    bare = _cls("Bare", blocking=False, returns=(("make", w),))
    assert contracts.wrap(_model(_cls("W"), bare))
    assert contracts.wrap(_model(_cls("W"))) == []


def test_an_enum_is_a_scalar_everywhere() -> None:
    """One rule, held by every layer that has an opinion: a StrEnum
    member IS a string, so an enum goes wherever a scalar goes -
    alone, in a list, in a map, and in a wire field.

    The codec half of this is tested where the codec lives; here is
    the half the generator decides."""
    from huggorm_gen import contracts, ir

    word = ir.TypeRef.named("Word", ir.Kind.ENUM)
    for t in (word, ir.TypeRef.list_of(word), ir.TypeRef.dict_of(word)):
        assert ir.wire_blocker(t, frozenset()) is None, t.spelling

    def probe(t: ir.TypeRef) -> Any:
        return _model(_cls("Probe", wire=Crossing.VALUE,
                           fields=(ir.FieldModel("kind", t),)),
                      enums=("Word",))

    for t in (word, ir.TypeRef.list_of(word)):
        assert contracts.wire(probe(t)) == [], t.spelling
    # ...and a field that cannot cross still fails, so the set widened
    # rather than the check weakening.
    complaints = contracts.wire(probe(ir.TypeRef.named("object", ir.Kind.OPAQUE)))
    assert len(complaints) == 1 and "not data" in complaints[0]


def test_an_optional_return_names_a_value_or_nothing(
        out: pathlib.Path) -> None:
    """`T | None` is a real return type, and only for some T.

    Absence is msgpack's nil, so any T may be optional: a scalar, a
    value, a container, an element of a container (huggorm#48,
    huggorm#142).

    A WRAPPED T is allowed too. Every layer adopts T when it is there
    and passes None through. The emitted async body is checked here,
    because a body that adopts None builds a wrapper around nothing
    and fails only at the first await on it."""
    from huggorm_gen import ir

    T = ir.TypeRef
    path = T.named("StorePath", ir.Kind.VALUE)
    served = frozenset({"Store"})
    for good in (path, T.named("str", ir.Kind.SCALAR), T.named("int", ir.Kind.SCALAR),
                 T.named("Word", ir.Kind.ENUM), T.named("Store", ir.Kind.PROXY)):
        assert ir.wire_blocker(T.optional_of(good), served) is None, good
    for shape in (T.optional_of(T.list_of(path)),
                  T.dict_of(T.optional_of(path)),
                  T.list_of(T.optional_of(path))):
        assert ir.wire_blocker(shape, served) is None, shape.spelling
    # An unserved proxy is refused whether or not it may be None.
    lost = T.named("Lost", ir.Kind.PROXY)
    for shape in (lost, T.optional_of(lost)):
        blocker = ir.wire_blocker(shape, served)
        assert blocker is not None and "no service" in blocker, shape

    # The corpus has the case: a Repl may hand back no Value.
    emitted = (out / "async_repl.py").read_text()
    assert "return None if result is None else AsyncValue._adopt(result, self._runner)" \
        in emitted, emitted
    assert "-> AsyncValue | None" in emitted, emitted

    # No pool class returns an affine one, plain or optional. An affine
    # class may (huggorm#8).
    from huggorm_gen import contracts

    made = ir.TypeRef.named("State", ir.Kind.PROXY)
    state = _cls("State", threading=Threading.AFFINE, returns=(("make", made),))
    for rt in (made, ir.TypeRef.optional_of(made)):
        pool = _cls("Pool", returns=(("make", rt),))
        assert contracts.affine_from_pool(_model(state, pool)), rt.spelling
    assert contracts.affine_from_pool(_model(state)) == []


def test_a_free_function_adopts_one_pool_object() -> None:
    """A free function's coroutine adopts its return through the pool,
    so an affine return, or a container of served objects, is refused
    before anything is emitted."""
    import dataclasses

    from huggorm_gen import contracts, ir

    def fn(name: str, returns: Any) -> Any:
        return ir.FunctionModel(name, "pkg.mod", Threading.POOL, (), returns,
                               "")

    pool = ir.TypeRef.named("Pool", ir.Kind.PROXY)
    state = ir.TypeRef.named("State", ir.Kind.PROXY)
    model = _model(_cls("Pool"), _cls("State", threading=Threading.AFFINE))
    for good in (pool, ir.TypeRef.optional_of(pool)):
        functions = {"make": fn("make", good)}
        assert contracts.free_functions(
            dataclasses.replace(model, functions=functions)) == []
    for bad, why in ((state, "needs a home thread"),
                     (ir.TypeRef.list_of(pool), "cannot be adopted")):
        functions = {"make": fn("make", bad)}
        complaints = contracts.free_functions(
            dataclasses.replace(model, functions=functions))
        assert len(complaints) == 1 and why in complaints[0], complaints


def test_runtime_contract(out: pathlib.Path) -> None:
    """The emitter-runtime import contract. Generated modules reference
    the runtime only via `from _runtime import X`; a rename on either
    side otherwise ships a wheel that fails at first wrapper import.
    Every referenced symbol must exist, and the core trio must still be
    exercised at all."""
    import huggorm_generated._runtime as rt
    referenced = set()
    for py in sorted(out.glob("*.py")):
        tree = ast.parse(py.read_text(), filename=str(py.name))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "_runtime":
                referenced |= {a.name for a in node.names}
    missing = sorted(n for n in referenced if not hasattr(rt, n))
    assert not missing, f"emitted modules import missing _runtime symbols: {missing}"
    assert {"BaseRunner", "unwrap_arg"} <= referenced, (
        f"emitters stopped importing the core runtime: {sorted(referenced)}"
    )


def test_each_wrapper_constructor_is_the_declared_one() -> None:
    """Every emitted wrapper __init__ states its declared constructor
    parameters and passes each one through unwrap_arg.

    Typed parameters leave no **kwargs to forward, so what can still go
    wrong is a parameter the emitter forgot to unwrap, or a signature
    that drifted from the declaration."""
    from huggorm_gen.cppgen.generate import declared_model

    model = declared_model()
    pkg_file = importlib.import_module("huggorm_generated").__file__
    assert pkg_file is not None
    pkg_dir = pathlib.Path(pkg_file).parent

    checked_ctors = 0
    abstract, not_wrapped = [], []
    for c in model.constructed:
        cls_name = c.name
        py = pkg_dir / f"async_{cls_name.lower()}.py"
        if c.wire is not Crossing.PROXY:
            # No handle was emitted, so there is no emitted __init__
            # to check. What must hold instead is that nothing was
            # emitted at all.
            assert not py.exists(), (
                f"{cls_name} is not a proxy, yet {py.name} exists")
            not_wrapped.append(cls_name)
            continue
        tree = ast.parse(py.read_text(), filename=str(py.name))
        init = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        )
        if not c.constructs:
            # A class with no door has one constructor, and it refuses.
            # Keyed on the DOOR, not on `abstract`: nix::Store states
            # the true C++ fact about itself AND keeps its factory.
            assert any(isinstance(n, ast.Raise) for n in ast.walk(init)), (
                f"{py.name}.__init__ must refuse to construct an abstract base"
            )
            abstract.append(cls_name)
            continue
        assert init.args.vararg is None and init.args.kwarg is None, (
            f"{py.name}.__init__ still takes *args/**kwargs"
        )
        declared = [p.name for p in c.ctor]
        emitted = [a.arg for a in init.args.args[1:]]  # drop self
        assert emitted == declared, (
            f"{py.name}.__init__ takes {emitted}, the declaration says {declared}"
        )
        unwrapped = {
            c.args[0].id
            for c in ast.walk(init)
            if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "unwrap_arg"
            and c.args and isinstance(c.args[0], ast.Name)
        }
        assert unwrapped == set(declared), (
            f"{py.name}.__init__ unwraps {sorted(unwrapped)}, "
            f"must unwrap every declared parameter {declared}"
        )
        n_optional = sum(1 for p in c.ctor if p.default is not None)
        assert len(init.args.defaults) == n_optional, (
            f"{py.name}.__init__ has {len(init.args.defaults)} default(s), "
            f"the declaration says {n_optional} optional parameter(s)"
        )
        checked_ctors += 1
    expected = len(model.constructed) - len(abstract) - len(not_wrapped)
    assert checked_ctors == expected, (
        f"checked {checked_ctors} wrapper ctors, expected {expected} "
        f"(abstract, so skipped: {abstract}; unwrapped: {not_wrapped})"
    )
    assert not_wrapped, "expected at least one unwrapped class in the surface"


def test_wrapping_and_wire_policy_are_separate_axes() -> None:
    """Immutable types are wire-values, everything else proxies. A class
    is wrapped when it needs a home thread (affine) or its methods can
    block. StorePath and PathInfo are pool and declare
    `_blocking = False`, so they cross every layer as themselves - no
    await in front of a substring read (huggorm#25)."""
    import huggorm_generated as flg
    from huggorm_gen.cppgen.generate import declared_model

    cls = declared_model().classes
    assert cls["StorePath"].wire is Crossing.VALUE
    assert cls["Value"].wire is Crossing.PROXY
    assert cls["PathInfo"].wire is Crossing.VALUE
    assert cls["EvalState"].wire is Crossing.PROXY
    for name in ("StorePath", "PathInfo"):
        assert not cls[name].blocking and not cls[name].wrapped, name
        assert not hasattr(flg, f"Async{name}"), f"Async{name} must not be generated"
        # With no handle to address, an rpc on it could never be called.
        assert not cls[name].served, name
    # The control: an affine class and a blocking pool class both stay
    # wrapped, so the rule is doing work rather than switching nothing.
    assert cls["Value"].wrapped is True
    assert cls["Store"].wrapped is True


def test_every_call_is_numbered_once() -> None:
    """A call crosses as its index into CALLS. The by-name tables must
    hold the same objects, or a client numbers a call the server reads
    as another."""
    from huggorm_generated._callspec import Acquire, Call
    from huggorm_generated._policy import ACQUIRE, CALLS, FREE, METHODS

    assert [c.index for c in CALLS] == list(range(len(CALLS)))
    named: list[Call | Acquire] = [*ACQUIRE.values(), *FREE.values(),
             *(m for ms in METHODS.values() for m in ms)]
    assert sorted(c.index for c in named) == list(range(len(CALLS)))
    for c in named:
        assert CALLS[c.index] is c, (c.index, c)


def test_the_wire_refuses_what_it_cannot_carry() -> None:
    """Every declared function is representable, so the corpus no longer
    exercises the blocker path. Exercised directly, or the mechanism
    that keeps an unrepresentable type out of the schema goes untested.

    A container of PROXIES stays refused whichever container it is, at
    any depth: one lease per element is not something anything grants
    in bulk. An opaque object and a module type with no wire spelling
    have no wire form at all. Containers nest freely (huggorm#142)."""
    from huggorm_gen import ir
    from huggorm_gen.cppgen.generate import declared_model

    free = declared_model().functions
    assert free["collect_garbage"].returns is None
    # gc_stats returns dict[str, int]: the declaration says what the
    # entries hold (huggorm#30).
    gc_return = free["gc_stats"].returns
    assert gc_return is not None and gc_return.spelling == "dict[str, int]"

    T = ir.TypeRef
    i, s = T.named("int", ir.Kind.SCALAR), T.named("str", ir.Kind.SCALAR)
    value, path = T.named("Value", ir.Kind.PROXY), T.named("StorePath", ir.Kind.VALUE)
    served = frozenset({"Value"})
    for t in (T.dict_of(value), T.list_of(value),
              T.list_of(T.dict_of(value)), T.dict_of(T.optional_of(value)),
              T.named("object", ir.Kind.OPAQUE),
              T.named("pathlib.Path", ir.Kind.MODULE)):
        assert ir.wire_blocker(t, served), f"{t.spelling} should be blocked"
    for t in (s, i, value, path, T.dict_of(i), T.dict_of(path),
              T.list_of(i), T.list_of(path), T.dict_of(T.dict_of(i)),
              T.dict_of(T.list_of(i)), T.list_of(T.list_of(i)),
              T.list_of(T.dict_of(path)),
              T.named("datetime.timedelta", ir.Kind.MODULE)):
        assert not ir.wire_blocker(t, served), (t.spelling,
                                                ir.wire_blocker(t, served))


def test_no_unused_imports(out: pathlib.Path) -> None:
    """Emitted modules must import exactly what they use. An import the
    emitter adds but never references means the import list is derived
    from the wrong set - which is how a sync binding class kept arriving
    in wrappers that only ever annotate the async one. Generated code
    gets no linter, so the gate lives here."""
    offenders = []
    for py in sorted(out.glob("*.py")):
        tree = ast.parse(py.read_text(), filename=str(py.name))
        imported = {
            (a.asname or a.name)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            # __future__ is a compiler directive, not a name to use.
            and node.module != "__future__"
            for a in node.names
        }
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        # __init__ re-exports rather than uses; __all__ names it instead.
        if py.name == "__init__.py":
            used |= {
                n.value
                for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            }
        unused = sorted(imported - used)
        if unused:
            offenders.append(f"{py.name}: {unused}")
    assert not offenders, "emitted modules with unused imports:\n" + "\n".join(offenders)


def _emitted_classes(out: pathlib.Path) -> dict[str, Any]:
    """Every class the build emitted: name -> (base names, methods).

    Read with ast rather than by importing, so the comparison is
    between what was WRITTEN in each of the three modules. An
    annotation that resolves to the same object through two different
    spellings is exactly the kind of drift this is looking for."""
    found: dict[str, Any] = {}
    for py in sorted(out.glob("*.py")):
        tree = ast.parse(py.read_text(), filename=py.name)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            methods = {}
            for f in node.body:
                if not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not is_surface(f.name):
                    continue
                args = f.args.args[1:]  # drop self
                methods[f.name] = {
                    "params": [a.arg for a in args],
                    "annotations": [ast.unparse(a.annotation) if a.annotation
                                    else None for a in args],
                    # Aligned to the END of the parameter list, the way
                    # Python aligns them, so a comparison across the
                    # three surfaces is between the same parameters.
                    "defaults": [ast.unparse(d) for d in f.args.defaults],
                    "returns": ast.unparse(f.returns) if f.returns else None,
                    "is_async": isinstance(f, ast.AsyncFunctionDef),
                    "where": py.name,
                }
            found[node.name] = ([b.id for b in node.bases
                                 if isinstance(b, ast.Name)], methods)
    return found


def _resolved(found: dict[str, Any], name: str) -> dict[str, Any]:
    """One class's whole method surface, leaf definitions winning."""
    bases, methods = found[name]
    out: dict[str, Any] = {}
    for b in bases:
        if b in found:
            out |= _resolved(found, b)
    merged: dict[str, Any] = out | methods
    return merged


def _expr(src: str) -> str:
    """One expression, normalized the way ast.unparse writes it, so a
    declared default and an emitted node compare as source."""
    return ast.unparse(ast.parse(src, mode="eval").body)


def test_the_declaration_is_what_got_written(out: pathlib.Path) -> None:
    """The emitted surfaces against the DECLARATION, not each other.

    test_conformance compares the three modules to one another, which
    is the right question for drift BETWEEN them and blind to drift
    they share. Emptying the emitter's defaults loop changes all three
    together, so they stay perfectly consistent and perfectly wrong -
    verified, and it passed.

    So this compares one surface to the declaration it came from. Only
    the parts that cross VERBATIM: parameter names and parameter
    defaults. An annotation is transformed on the way out - widened
    for async, renamed to a protocol, swapped for a twin - and
    re-deriving those here would rebuild the emitter inside its own
    test, which is exactly what test_conformance avoids. A name and a
    default get no such treatment, so comparing them needs no rules at
    all.

    The in-process wrapper is the surface picked, because it is the
    one that carries every method: the protocol drops what it cannot
    promise and the rpc client drops what cannot cross."""
    from huggorm_gen.cppgen.generate import declared_model

    model = declared_model()
    served = model.ordered_served
    found = _emitted_classes(out)

    failures, checked, ctor_checked = [], 0, 0
    for c in served:
        emitted = _resolved(found, c.async_name)
        for m in c.methods:
            sig = emitted.get(m.name)
            if sig is None:
                failures.append(f"{c.name}.{m.name}: declared, not emitted")
                continue
            checked += 1
            want_names = [p.name for p in m.params]
            if sig["params"] != want_names:
                failures.append(
                    f"{c.name}.{m.name} takes {sig['params']}, the "
                    f"declaration says {want_names}")
            # Defaults align to the END of the parameter list, the way
            # Python aligns them.
            want_defaults = [_expr(p.default) for p in m.params
                             if p.default is not None]
            if [_expr(d) for d in sig["defaults"]] != want_defaults:
                failures.append(
                    f"{c.name}.{m.name} defaults to {sig['defaults']}, the "
                    f"declaration says {want_defaults}")

    # ...and the CONSTRUCTORS, read off the stubs. That is where a
    # constructor default actually lands: the only class with one is a
    # returned type, and a returned type's emitted __init__ takes
    # (obj, runner) rather than the declared parameters. A check
    # against the wrappers alone would have compared two empty lists
    # and said nothing.
    from huggorm_gen.pygen.emitter import STUB_PACKAGE

    # Every declared class, not only the served ones: the stubs
    # describe the BINDINGS.
    declared_classes = model.classes
    for pyi in sorted((out.parent / STUB_PACKAGE).glob("*.pyi")):
        tree = ast.parse(pyi.read_text(), filename=pyi.name)
        for node in tree.body:
            if (not isinstance(node, ast.ClassDef)
                    or node.name not in declared_classes):
                continue
            init = next((f for f in node.body
                         if isinstance(f, ast.FunctionDef)
                         and f.name == "__init__"), None)
            if init is None:
                continue
            want = [_expr(p.default)
                    for p in declared_classes[node.name].ctor
                    if p.default is not None]
            got = [_expr(ast.unparse(d)) for d in init.args.defaults]
            if got != want:
                failures.append(
                    f"{pyi.name}:{node.name}.__init__ defaults to {got}, "
                    f"the declaration says {want}")
            ctor_checked += 1

    assert not failures, ("the emitted surface and the declaration "
                          "disagree:\n  " + "\n  ".join(failures))
    assert any(p.default is not None
               for c in declared_classes.values() for p in c.ctor), (
        "no constructor declares a default; that half proves nothing")
    assert ctor_checked >= len(served) // 2, (
        f"checked only {ctor_checked} constructor(s) in the stubs")
    assert checked >= 3 * len(served), (
        f"checked only {checked} method(s) across {len(served)} classes")
    # Non-vacuity: something must actually HAVE a default, or the
    # comparison is between two empty lists everywhere.
    assert any(p.default is not None
               for c in declared_classes.values()
               for m in c.methods for p in m.params), (
        "no method declares a default; this gate now proves nothing")


def test_conformance(out: pathlib.Path) -> None:
    """The three emitted surfaces must agree.

    A protocol is only worth having if the implementations really
    satisfy it, and isinstance() against a runtime_checkable Protocol
    checks method NAMES and nothing else - an implementation whose
    parameters drifted still passes. So this compares signatures, and
    it compares the three modules against EACH OTHER rather than
    against a rederivation of what the emitter should have written.

    The rules:
      - the async and rpc implementations offer the same method names;
      - the rpc client and the protocol offer those minus the ones
        that cannot cross the wire;
      - parameter names and annotations are identical in all three
        (which is what huggorm#25 bought: after it, a method on the
        protocol mentions no type that differs by location);
      - a return is identical in all three, unless the protocol names
        another protocol - then each implementation must return ITS
        form of that same class, or of that class or None."""
    from huggorm_gen.cppgen.generate import declared_model

    model = declared_model()
    # Served, not wrapped: every proxy has all three surfaces, and the
    # gate compares all three of each.
    served = {c.name: c for c in model.ordered_served}
    # protocol name -> the class it speaks for, so a protocol-typed
    # return can be checked against each implementation's own form.
    speaks_for = {c.protocol_name: n for n, c in served.items()}
    found = _emitted_classes(out)

    def no_wire_of(c: Any) -> set[str]:
        """What the rpc client cannot offer - and so neither can the
        protocol, which is what both implementations satisfy."""
        return {m.name for m in c.methods if not model.offered(m)}

    failures, checked = [], 0
    for cls_name, cls in served.items():
        P = _resolved(found, cls.protocol_name)
        A = _resolved(found, cls.async_name)
        R = _resolved(found, cls.rpc_name)
        no_wire = no_wire_of(cls)

        if set(R) != set(A) - no_wire:
            # The in-process surface is the larger one: a method the
            # wire cannot carry keeps its wrapper and is absent from
            # the client. Anything else is drift.
            failures.append(
                f"{cls_name}: in-process offers {sorted(set(A) - set(R))} "
                f"the rpc client does not, and {sorted(set(R) - set(A))} "
                f"the other way; {sorted(no_wire)} cannot cross the wire")
        if set(P) != set(A) - no_wire:
            failures.append(
                f"{cls_name}: {cls.protocol_name} offers {sorted(P)}; the "
                f"implementations offer {sorted(A)} and {sorted(no_wire)} "
                f"cannot cross the wire")

        for m in sorted(P):
            checked += 1
            want = P[m]
            for label, sig in (("in-process", A.get(m)), ("rpc", R.get(m))):
                if sig is None:
                    failures.append(f"{cls_name}.{m}: no {label} implementation")
                    continue
                if sig["params"] != want["params"]:
                    failures.append(
                        f"{cls_name}.{m}: {label} takes {sig['params']}, "
                        f"{cls.protocol_name} declares {want['params']}")
                if sig["annotations"] != want["annotations"]:
                    failures.append(
                        f"{cls_name}.{m}: {label} annotates "
                        f"{sig['annotations']}, {cls.protocol_name} declares "
                        f"{want['annotations']}")
                if sig["defaults"] != want["defaults"]:
                    # A default is part of what a call MEANS. Three
                    # surfaces that agree on types and disagree here
                    # answer the same short call differently depending
                    # on where the object lives.
                    failures.append(
                        f"{cls_name}.{m}: {label} defaults to "
                        f"{sig['defaults']}, {cls.protocol_name} declares "
                        f"{want['defaults']}")
                if not sig["is_async"]:
                    failures.append(f"{cls_name}.{m}: {label} is not async")
                expected = want["returns"]
                held = expected.removesuffix(" | None")
                if held in speaks_for:
                    c = served[speaks_for[held]]
                    expected = expected.replace(
                        held, c.async_name if label == "in-process" else c.rpc_name)
                elif label == "in-process":
                    # A declared async twin is the same value in the
                    # other spelling - anyio.Path wraps a pathlib.Path
                    # to give it awaitable methods - so the in-process
                    # wrapper hands back the twin and that is not
                    # drift. The remote client keeps the plain one: it
                    # has no local file either way.
                    # A protocol method the declaration does not
                    # declare, such as `close`, returns no twin.
                    returned = {x.name: x.returns for x in cls.methods}.get(m)
                    if returned is not None and returned.leaf.twin:
                        expected = expected.replace(returned.leaf.name,
                                                    returned.leaf.twin)
                if sig["returns"] != expected:
                    failures.append(
                        f"{cls_name}.{m}: {label} returns {sig['returns']}, "
                        f"expected {expected} for {want['returns']}")

    assert not failures, ("the generated surfaces disagree:\n  "
                          + "\n  ".join(failures))
    # Non-vacuity: the gate must have had something to compare, and the
    # blocked set must be real rather than an empty rule.
    assert checked >= 3 * len(served), (
        f"conformance checked only {checked} method(s) across "
        f"{len(served)} classes")
    assert any(no_wire_of(c) for c in served.values()), (
        "no method is blocked from the wire; either the rule stopped "
        "working or the surface changed and this gate now proves nothing")


def test_a_free_function_adopts_its_proxy(out: pathlib.Path) -> None:
    """A free function hands back the Async form of a proxy it makes.

    `test_conformance` walks classes, so it never sees a free
    function. A coroutine annotated with the sync proxy hands a sync
    object to an async caller, and the server leases that object as a
    handle whose methods it then awaits."""
    from huggorm_gen import ir
    from huggorm_gen.cppgen.generate import declared_model

    model = declared_model()
    emitted = {
        node.name: ast.unparse(node.returns) if node.returns else "None"
        for node in ast.parse(
            (out / "free_functions.py").read_text()).body
        if isinstance(node, ast.AsyncFunctionDef)
    }
    failures, adopted = [], 0
    for name, fn in model.functions.items():
        if not fn.wrapped:
            continue
        rt = fn.returns
        expected = rt.spelling if rt is not None else "None"
        if rt is not None and rt.origin in (None, ir.Origin.OPTIONAL) \
                and rt.kind == ir.Kind.PROXY:
            adopted += 1
            expected = expected.replace(rt.name, f"Async{rt.name}")
        if _expr(emitted[name]) != _expr(expected):
            failures.append(f"{name}: emitted {emitted[name]}, "
                            f"expected {expected}")
    assert not failures, "\n  ".join(failures)
    assert adopted, "no free function returns a proxy; this gate is vacuous"


def test_stubs(out: pathlib.Path) -> None:
    """The stub package must describe the bindings EXACTLY.

    A stub package is authoritative: once huggorm_bindings-stubs exists, a
    typechecker stops looking at the real module, so a name the stubs
    omit becomes an error at every call site and a name they invent
    becomes a call that fails at runtime. Both directions matter, which
    is why this compares sets rather than checking coverage one way.

    Reflected against the LIVE modules, not the model. The model
    holds the generated surface, which the affine-return drop and 018's
    hierarchy split have already filtered; the bindings have neither."""
    import importlib

    from huggorm_gen.pygen.emitter import STUB_PACKAGE

    stub_dir = out.parent / STUB_PACKAGE
    assert stub_dir.is_dir(), f"no stub package at {stub_dir}"

    bindings = importlib.import_module("huggorm_bindings")
    failures = []
    checked = 0
    for pyi in sorted(stub_dir.glob("*.pyi")):
        if pyi.name == "__init__.pyi":
            continue
        module = importlib.import_module(f"{bindings.__name__}.{pyi.stem}")
        tree = ast.parse(pyi.read_text(), filename=pyi.name)

        declared, classes = set(), {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                declared.add(node.name)
                classes[node.name] = node
            elif isinstance(node, ast.FunctionDef):
                declared.add(node.name)
        live = {n for n in dir(module) if not n.startswith("_")
                and getattr(getattr(module, n), "__module__", None) == module.__name__}
        if declared != live:
            failures.append(
                f"{pyi.name}: declares {sorted(declared - live)} that do not "
                f"exist, and omits {sorted(live - declared)}")

        # Every name an annotation uses must be one the stub binds. A
        # stub that names a union alias the module never defines -
        # which every union return did - reads to a typechecker as an
        # unknown type, and nothing else here notices.
        bound = set(declared) | set(dir(builtins))
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
        used = set()
        for walked in ast.walk(tree):
            annotations = []
            if isinstance(walked, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotations += [a.annotation for a in (
                    *walked.args.posonlyargs, *walked.args.args,
                    *walked.args.kwonlyargs) if a.annotation is not None]
                if walked.returns is not None:
                    annotations.append(walked.returns)
            elif isinstance(walked, ast.AnnAssign):
                annotations.append(walked.annotation)
            for ann in annotations:
                used |= {n.id for n in ast.walk(ann) if isinstance(n, ast.Name)}
        if used - bound:
            failures.append(
                f"{pyi.name}: annotations name {sorted(used - bound)}, which "
                f"the stub neither defines nor imports")

        # Everything the stub says a class has, including what it
        # inherits through a base the stub also declares. Defined once
        # over `classes` rather than rebuilt inside the loop: a closure
        # written in a loop body reads as a deferred capture of the
        # loop variable, which is a bug in every case but this one.
        def surface(n: str, classes: dict[str, ast.ClassDef] = classes) -> set[str]:
            out_: set[str] = set()
            cn = classes.get(n)
            if cn is None:
                return out_
            for b in cn.bases:
                if isinstance(b, ast.Name):
                    out_ |= surface(b.id)
            return out_ | {f.name for f in cn.body
                           if isinstance(f, ast.FunctionDef)
                           and is_surface(f.name)}

        for name in classes:
            cls = getattr(module, name)
            stub_methods = surface(name)
            live_methods = set()
            for k in cls.__mro__:
                if getattr(k, "__module__", "").split(".")[0] != bindings.__name__:
                    continue
                live_methods |= {a for a, v in k.__dict__.items()
                                 if is_surface(a)
                                 and (callable(v) or hasattr(v, "__get__"))}
            if stub_methods != live_methods:
                failures.append(
                    f"{pyi.name}:{name} declares "
                    f"{sorted(stub_methods - live_methods)} that do not "
                    f"exist, and omits {sorted(live_methods - stub_methods)}")
            checked += 1

    assert not failures, "stubs disagree with the bindings:\n  " + "\n  ".join(failures)
    assert checked >= 5, f"stub gate checked only {checked} class(es)"

    # The __init__ stub must re-export exactly what the real one does.
    init = ast.parse((stub_dir / "__init__.pyi").read_text())
    exported = {
        a.name
        for node in init.body if isinstance(node, ast.ImportFrom)
        for a in node.names
    }
    assert exported == set(bindings.__all__), (
        f"__init__.pyi exports {sorted(exported)}, the package exports "
        f"{sorted(bindings.__all__)}")
    # A plain import in a stub is PRIVATE; only `X as X` re-exports.
    aliased = {
        a.name
        for node in init.body if isinstance(node, ast.ImportFrom)
        for a in node.names if a.asname == a.name
    }
    assert aliased == exported, (
        f"these are imported but not re-exported: {sorted(exported - aliased)}")


def test_docstrings() -> None:
    """Every emitted module and class must carry a real __doc__. A
    string literal is only a docstring when nothing precedes it, so an
    import or a class attribute emitted first silently demotes it to a
    dead expression - the generated surface then documents itself to
    nobody, help() included."""
    import huggorm_generated as flg

    missing = []
    if not (flg.__doc__ or "").strip():
        missing.append("huggorm_generated")
    for name in flg.__all__:
        cls = getattr(flg, name)
        if not isinstance(cls, type) and not callable(cls):
            continue  # a re-exported registry, not a documented object
        mod = sys.modules[cls.__module__]
        if not (mod.__doc__ or "").strip():
            missing.append(f"module {cls.__module__}")
        if not (cls.__doc__ or "").strip():
            missing.append(f"class {name}")
    assert not missing, f"emitted objects without a docstring: {sorted(set(missing))}"


def test_annotations_resolve() -> None:
    """PEP 649 defers annotation evaluation, so a missing import only
    explodes when something calls typing.get_type_hints - which every
    introspecting consumer does, and every Python below 3.14 does at
    class creation time. Force resolution over the whole surface."""
    import typing

    import huggorm_generated as flg

    failures = []
    for name in flg.__all__:
        cls = getattr(flg, name)
        if not isinstance(cls, type) and not callable(cls):
            continue  # a re-exported registry, not an annotated object
        targets = [(name, cls)]
        for attr, val in vars(cls).items():
            fn = val.__func__ if isinstance(val, (staticmethod, classmethod)) else val
            if callable(fn):
                targets.append((f"{name}.{attr}", fn))
        for label, obj in targets:
            try:
                typing.get_type_hints(obj)
            except Exception as e:
                failures.append(f"{label}: {type(e).__name__}: {e}")
    assert not failures, "unresolvable annotations:\n" + "\n".join(failures)


def _rendered(sig: str) -> str:
    """One nanobind signature, as (types) -> type.

    Parameter NAMES are dropped and so are defaults. nanobind renders
    a default as an opaque `\\0` placeholder, and the names are the
    declaration's own - `"name"_a` comes straight off it, so comparing
    them would compare the declaration with itself."""
    body = sig[sig.index("(") + 1:sig.rindex(")")]
    ret = sig[sig.rindex(")") + 1:].removeprefix(" -> ")
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    typed = [p.split(":", 1)[1].split("=")[0] if ":" in p else p
             for p in (q.strip() for q in parts)
             if p and p.strip() != "self"]
    return f"({', '.join(_same(x) for x in typed)}) -> {_same(ret)}"


def _same(spelling: str) -> str:
    """One type, in the spelling both sides can be compared in.

    Three differences are real and none of them is a disagreement:

    - nanobind qualifies a bound class by its module, because that is
      what a caller must import. `StorePath` and
      `huggorm_bindings.path.StorePath` are one type. `pathlib.Path`
      keeps its module, because it is not one of ours.
    - a caster takes a `Sequence` and answers a `list`, and takes a
      `Mapping` and answers a `dict`. The declaration says `list` and
      `dict` for both, which is the surface a caller sees.
    - a VOCABULARY is a StrEnum whose members ARE the strings
      libstore parses, so it crosses as `str` and nanobind says so.
      That is the whole point of declaring it as words rather than
      binding it, and `_VOCABULARIES` is read from the model
      rather than listed here.
    - a UNION is an alias, and nanobind renders the arms it actually
      binds. `SingleDerivedPath` is `StorePath |
      SingleDerivedPathBuilt` by declaration, so the alias expands to
      exactly that and the two sides meet. Expanded REPEATEDLY,
      because an arm may itself name one - `_UNIONS` is read from the
      model, so nothing here lists an alias by hand.
    - an EXCEPTION a value holds crosses as `nb::object`, so nanobind
      says `object` and can say nothing else: a Python exception is
      not a bound C++ type and has no signature to render. The `|
      None` goes with it, because `nb::none()` IS the absent value
      there and the emitter writes no `std::optional` around it.
      `_ERRORS` is read from the model, like the two above.

    A DURATION needed none of this, which was measured rather than
    assumed. nanobind's chrono caster reads `datetime.timedelta |
    float` and writes `datetime.timedelta`, so a PARAMETER of one
    would disagree with the declaration - and no gate here compares
    one, because the only duration parameters are `_from_parts`'s and
    an `_`-prefixed name reaches no stub. A collapse of the input
    spelling was written here, passed, and was removed when taking it
    out changed nothing (huggorm#71)."""
    from huggorm_gen.payload.wiretypes import python_spelling

    out = re.sub(r"huggorm_bindings\.\w+\.", "", spelling)
    out = out.replace("collections.abc.Sequence[", "list[")
    out = out.replace("collections.abc.Mapping[", "dict[")
    for name in _ERRORS:
        out = re.sub(rf"\b{re.escape(name)}\b(\s*\|\s*None)?", "object", out)
    for name in _VOCABULARIES:
        out = re.sub(rf"\b{re.escape(name)}\b", "str", out)
    for _ in range(len(_UNIONS) + 1):
        before = out
        for alias, arms in _UNIONS.items():
            out = re.sub(rf"\b{re.escape(alias)}\b",
                         " | ".join(python_spelling(a) for a in arms), out)
        if out == before:
            break
    return re.sub(r"\s+", " ", out).strip()


_VOCABULARIES: set[str] = set()
# {alias: [arm, ...]}, from the model. See `_same`.
_UNIONS: dict[str, list[str]] = {}
# Declared EXCEPTION classes, from the model. See `_same`.
_ERRORS: set[str] = set()


def test_a_declared_type_is_the_type_nanobind_BINDS(
        out: pathlib.Path) -> None:
    """The declaration and the real C++ must agree, per method.

    The gate above catches a type nanobind cannot cast at all. This
    catches the other half: a declaration that says one thing while
    the C++ says another, where a caster happens to exist so nothing
    complains.

    The two gates split the space cleanly, and it is worth writing
    down which half each one holds, because it is not obvious.

    Caster includes are per TRANSLATION UNIT: `includes()` derives
    them from every declared type in the file, so ONE method declaring
    `StrView` pulls <nanobind/stl/string_view.h> in for every method
    beside it. A sibling that says `Str` where the C++ takes
    `std::string_view` then renders on the back of it - and it renders
    CORRECTLY, as `str`, because a caster exists and both spellings
    are `str` to Python. Measured: change one of the two and this gate
    stays green, rightly. Nothing a caller sees is wrong. Change both
    and no caster is included at all, which is the `::` gate's half.

    What this half holds is a declared PYTHON type that differs from
    the real one, and it is not cosmetic, because every surface above
    believes it. `Value.size` answers an `int` and
    was declared `Bint`: the stub said bool, the message carried
    `bool result = 1`, and a count of three crossed the wire as True.
    The C++ compiled, because nanobind casts an int to a Python bool
    without complaint.

    nanobind renders each signature from the C++ it actually calls, so
    it is the honest side of this comparison. Where the two disagree,
    the declaration is the one to fix."""
    from huggorm_gen.cppgen.generate import declared_model

    model = declared_model()
    _VOCABULARIES.clear()
    _VOCABULARIES.update(model.enums)
    _UNIONS.clear()
    _UNIONS.update({n: [a.name for a in u.arms]
                    for n, u in model.unions.items()})
    _ERRORS.clear()
    _ERRORS.update(model.errors.classes)

    bad, checked = [], 0
    for c in model.classes.values():
        cls = getattr(importlib.import_module(c.qualified_module), c.name)
        for meth in c.methods:
            fn = getattr(cls, meth.name, None)
            sigs = getattr(fn, "__nb_signature__", None)
            if not sigs:
                continue
            params = [_same(p.type.spelling)
                      # A parameter that reads None arrives as a
                      # std::optional so an explicit None works, and
                      # nanobind says so. The declaration says it with
                      # the default.
                      + (" | None" if p.defaults_to_none
                         and not p.type.optional else "")
                      for p in meth.params]
            want = f"({', '.join(params)}) -> {_same(meth.return_spelling)}"
            got = _rendered(sigs[0][0])
            checked += 1
            if got != want:
                bad.append(f"{c.name}.{meth.name}\n"
                           f"      nanobind: {got}\n"
                           f"      declared: {want}")
    assert not bad, (
        "the declaration disagrees with the C++ nanobind binds. nanobind "
        "reads the real signature, so the declaration is what to fix:\n    "
        + "\n    ".join(bad))
    assert checked, "no bound signature was found to check"


def test_no_binding_leaks_a_cxx_type(out: pathlib.Path) -> None:
    """Every bound signature must be spelled in PYTHON types.

    nanobind renders each binding's signature from the REAL C++ it
    calls, and it can only render a type it has a caster for. Where it
    has none, it prints the C++ spelling instead and refuses the call
    at run time:

        def parse_store_path(
            self, path: "std::basic_string_view<char, ...>") -> StorePath

    That is the failure this gate exists for, because nothing else
    catches it. The C++ compiles - the method pointer is valid. The
    declaration typechecks. The stub says `str`, the model says
    `str`, and a caller gets TypeError on the first call saying the
    supported argument type is a C++ type they have never heard of.

    It happened: `parseStorePath` and `followLinksToStorePath` take
    `std::string_view` (store-dir-config.hh:37, store-api.hh:370)
    while the declaration said `Str`. `includes()` derives the caster
    headers from the DECLARED types, so nothing pulled in
    <nanobind/stl/string_view.h> and both methods were unreachable.

    The check is one character, and that is the point: a `::` in a
    rendered signature is always a caster the emitter did not include.
    A test would catch it for a method that has one; this catches it
    for every method at once."""
    from huggorm_gen.cppgen.generate import declared_model

    bad, seen = [], 0
    for c in declared_model().classes.values():
        cls = getattr(importlib.import_module(c.qualified_module), c.name)
        for name in [m.name for m in c.methods] + ["__init__"]:
            fn = getattr(cls, name, None)
            for sig, *_ in getattr(fn, "__nb_signature__", None) or ():
                seen += 1
                if "::" in sig:
                    bad.append(f"{c.name}.{name}: {sig}")
    assert not bad, (
        "a bound signature names a C++ type, so nanobind has no caster "
        "for it and every call raises TypeError:\n  " + "\n  ".join(bad))
    assert seen, "no bound signature was found to check"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = pathlib.Path(args.out).resolve()

    # Import the generated package from its parent dir, shadowing any
    # installed copy. Bindings (huggorm_bindings) come from PYTHONPATH.
    # setup.py runs the generator before this, so the package is
    # already there and the earlier checks lose nothing by the path
    # being set first.
    sys.path.insert(0, str(out.parent))
    importlib.invalidate_caches()

    # DISCOVERED, not listed. This was a hand-written call list, and a
    # test added later was simply not in it: two of them existed here,
    # linted, typechecked, and never ran. A gate nothing calls is
    # worse than no gate, because the file says it is covered.
    # Definition order is the run order: a module's dict keeps it.
    checks = [fn for name, fn in list(globals().items())
              if name.startswith("test_") and callable(fn)]

    def call(fn: Any) -> Any:
        # Every check takes the output directory or nothing, and the
        # signature says which - so adding one needs no registration.
        return fn(out) if inspect.signature(fn).parameters else fn()

    for fn in checks:
        call(fn)
    print(f"smoke test OK ({len(checks)} checks)")


if __name__ == "__main__":
    main()
