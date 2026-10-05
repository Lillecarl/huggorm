"""
Verify the freshly generated package. Installed as the `codegen-smoke`
entry point; runs after codegen-generate, stdlib only:

1. every emitted .py parses
2. the package imports and __all__ matches
3. every emitted module and class carries a real docstring, and
   imports exactly the names it uses
4. behavioral checks: results, C++ exception wrapping, affine thread
   pinning (including returned affine values), pool execution,
   policy-driven surface drops, aclose, exactly-once lazy construction
5. the emitter-runtime symbol contract: every name any emitted module
   imports from _runtime must exist on the runtime module
"""

import argparse
import ast
import builtins
import gc
import importlib
import inspect
import pathlib
import re
import sys
from typing import Any

import anyio

from huggorm_dsl.read import is_surface


def test_parse(out: pathlib.Path) -> None:
    for py in sorted(out.glob("*.py")):
        ast.parse(py.read_text(), filename=str(py))


def _cls(name: str, *, threading: str = "pool", blocking: bool = True,
         wire: str = "", fields: tuple[Any, ...] = (),
         returns: tuple[tuple[str, Any], ...] = ()) -> Any:
    """One class model, built by hand for a contract the corpus does
    not break."""
    from huggorm_dsl.declare import Decl
    from huggorm_gen import ir

    return ir.ClassModel(
        name=name, package="pkg", module="mod", doc="",
        decl=Decl(name=name, threading=threading, blocking=blocking,
                  wire=wire),
        is_value=wire == "value", produced=False, constructs=True,
        wire_fields=fields, ctor=(),
        methods=tuple(ir.MethodModel(m, (), t, "") for m, t in returns))


def _model(*classes: Any, enums: tuple[str, ...] = ()) -> Any:
    from huggorm_gen import ir

    return ir.Model({c.name: c for c in classes}, {}, {}, frozenset(), {},
                    {n: ir.EnumModel(n, "pkg.mod", (), "") for n in enums},
                    ir.Errors("", {}))


def test_a_container_of_wrapped_types_is_refused() -> None:
    """A container of wrapped types builds, emits a schema, and then
    hands back bare sync objects: nothing attaches a runner to
    elements."""
    from huggorm_gen import contracts, ir

    v = ir.TypeRef.named("V", "proxy")
    model = _model(_cls("V", returns=(
        ("attrs", ir.TypeRef.dict_of(v)), ("items", ir.TypeRef.list_of(v)),
        ("one", v), ("maybe", ir.TypeRef.optional_of(v)),
        ("n", ir.TypeRef.named("int", "scalar")))))
    bad = contracts.collection(model)
    assert len(bad) == 2, bad
    assert all("attrs" in b or "items" in b for b in bad), bad


def test_an_unwrapped_class_hands_back_nothing_wrapped() -> None:
    """A pool class that cannot block is not wrapped, so a caller holds
    its sync object - and a wrapped return would arrive with no
    runner."""
    from huggorm_gen import contracts, ir

    w = ir.TypeRef.named("W", "proxy")
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

    word = ir.TypeRef.named("Word", "enum")
    for t in (word, ir.TypeRef.list_of(word), ir.TypeRef.dict_of(word)):
        assert ir.wire_blocker(t, frozenset()) is None, t.spelling

    def probe(t: ir.TypeRef) -> Any:
        return _model(_cls("Probe", wire="value",
                           fields=(ir.FieldModel("kind", t),)),
                      enums=("Word",))

    for t in (word, ir.TypeRef.list_of(word)):
        assert contracts.wire(probe(t)) == [], t.spelling
    # ...and a field that cannot cross still fails, so the set widened
    # rather than the check weakening.
    complaints = contracts.wire(probe(ir.TypeRef.named("object", "opaque")))
    assert len(complaints) == 1 and "not data" in complaints[0]


def test_an_optional_return_names_a_value_or_nothing(
        out: pathlib.Path) -> None:
    """`T | None` is a real return type, and only for some T.

    Absence rides on presence, which is one bit. So it separates ONE
    type from nothing: a union of two real types has no field to put
    either arm in.

    A scalar is allowed, and used not to be. proto3 has had explicit
    `optional` since 3.15 - a synthetic oneof gives a scalar field
    real presence - so the old refusal described what this schema
    builder emitted rather than what proto3 can say (huggorm#48).

    A WRAPPED T is allowed too. A Handle is a message, so the wire has
    presence already, and every layer adopts T when it is there and
    passes None through. The emitted async body is checked here,
    because a body that adopts None builds a wrapper around nothing
    and fails only at the first await on it."""
    from huggorm_gen import ir

    T = ir.TypeRef
    path = T.named("StorePath", "value")
    served = frozenset({"Store"})
    for good in (path, T.named("str", "scalar"), T.named("int", "scalar"),
                 T.named("Word", "enum"), T.named("Store", "proxy")):
        assert ir.wire_blocker(T.optional_of(good), served) is None, good
    blocker = ir.wire_blocker(T.optional_of(T.list_of(path)), served)
    assert blocker is not None and "IS an empty one" in blocker, blocker
    # The element's own None, not a missing type: StorePath is a value.
    for shape in (T.dict_of(T.optional_of(path)),
                  T.list_of(T.optional_of(path))):
        blocker = ir.wire_blocker(shape, served)
        assert blocker is not None and "has no presence" in blocker, blocker
    # An unserved proxy is refused whether or not it may be None.
    lost = T.named("Lost", "proxy")
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

    made = ir.TypeRef.named("State", "proxy")
    state = _cls("State", threading="affine", returns=(("make", made),))
    for rt in (made, ir.TypeRef.optional_of(made)):
        pool = _cls("Pool", returns=(("make", rt),))
        assert contracts.affine_from_pool(_model(state, pool)), rt.spelling
    assert contracts.affine_from_pool(_model(state)) == []


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
    assert {"attach_runner", "unwrap_arg"} <= referenced, (
        f"emitters stopped importing the core runtime: {sorted(referenced)}"
    )


async def _all(*aws: Any) -> list[Any]:
    """Every awaitable concurrently, results in order.

    anyio has no `gather`, and that absence is the point: a task
    group OWNS its children, so a failure in one cancels the rest
    rather than being handed back as a value nobody looks at.
    Ordering by index is the only thing `gather` gave that a task
    group does not, so it is the only thing restated here."""
    out: list[Any] = [None] * len(aws)

    async def one(i: int, aw: Any) -> None:
        out[i] = await aw

    async with anyio.create_task_group() as tg:
        for i, aw in enumerate(aws):
            tg.start_soon(one, i, aw)
    return out


async def _all_errors(*aws: Any) -> list[Any]:
    """As `_all`, but every awaitable is EXPECTED to fail.

    `gather(return_exceptions=True)` in one call. Separate from `_all`
    because a task group's default is the opposite - the first
    failure cancels its siblings - and a flag that inverts a task
    group's whole contract reads better as a second name.

    `Exception` and not `BaseException`: swallowing the cancellation
    exception is the one thing anyio's contract forbids."""
    out: list[Any] = [None] * len(aws)

    async def one(i: int, aw: Any) -> None:
        try:
            await aw
        except Exception as exc:
            out[i] = exc

    async with anyio.create_task_group() as tg:
        for i, aw in enumerate(aws):
            tg.start_soon(one, i, aw)
    return out


async def test_behavior() -> None:
    import tempfile

    import huggorm_bindings
    from huggorm_bindings import ContentAddressMethod as CA
    from huggorm_bindings import HashAlgorithm, StorePath
    from huggorm_bindings.errors import NixTypeError
    from huggorm_generated import (
        AsyncEvalState,
        AsyncStore,
        AsyncValue,
    )
    from huggorm_generated._runtime import InternalError

    def added(name: str, body: bytes) -> tuple[str, bytes]:
        return name, body

    from huggorm_gen.cppgen.generate import declared_model

    model = declared_model()
    # The emitted package, for the wrapper sources this reads back.
    pkg_file = importlib.import_module("huggorm_generated").__file__
    assert pkg_file is not None
    pkg_dir = pathlib.Path(pkg_file).parent

    # Pool store: concurrent adds genuinely overlap. A chroot store
    # rather than dummy://, because this adds paths and dummy:// holds
    # none - and a chroot needs no daemon, which is what lets it run
    # inside the build sandbox.
    root = tempfile.mkdtemp(prefix="huggorm-smoke-")
    local = AsyncStore(root)
    assert await local.query_all_valid_paths() == [], "a fresh chroot is empty"
    t0 = anyio.current_time()
    p1, p2 = await _all(
        local.add_to_store("hello.txt", b"world", CA.NAR, HashAlgorithm.SHA256),
        local.add_to_store("note.txt", b"nix real", CA.NAR, HashAlgorithm.SHA256),
    )
    elapsed = anyio.current_time() - t0
    assert elapsed < 0.18, f"expected overlapped adds, took {elapsed:.2f}s"
    # StorePath is pool AND non-blocking, so it has no wrapper: an
    # awaited store method hands back the binding object itself, and
    # reading it is a plain call (huggorm#25).
    assert type(p1) is StorePath
    assert len({p1.to_string(), p2.to_string()}) == 2
    assert await local.is_valid_path(p1) is True

    # Lazy construction must run the factory EXACTLY ONCE, even when
    # concurrent first-calls hit one pool handle. Regression guard for
    # the unlocked _resolve race (each stray factory call once produced
    # diverging underlying stores).
    from huggorm_generated import _runtime

    class _Probe:
        def noop(self) -> str:
            return "ok"

    made: list[int] = []

    def factory() -> _Probe:
        made.append(1)
        return _Probe()

    runner = _runtime.PoolRunner(factory)
    results = await _all(*(runner.call("noop", []) for _ in range(8)))
    assert results == ["ok"] * 8
    assert len(made) == 1, f"factory ran {len(made)}x under concurrent first-calls"

    # Same guarantee on the failure path: one attempt, every caller
    # gets the cached error.
    failed: list[int] = []

    def bad_factory() -> object:
        failed.append(1)
        raise RuntimeError("no")

    bad_runner = _runtime.PoolRunner(bad_factory)
    errs = await _all_errors(*(bad_runner.call("noop", []) for _ in range(4)))
    assert len(failed) == 1, f"failing factory ran {len(failed)}x"
    assert all(isinstance(e, InternalError) for e in errs)
    assert all(type(e.__cause__) is RuntimeError for e in errs)

    # Cross-thread unwrap guard: an affine wrapper that has never been
    # called refuses construction on a foreign thread. Silent off-home
    # construction was the bug (ensure() used to build affine objects
    # wherever the caller happened to run).
    import types

    def _shell(runner: Any) -> Any:
        return types.SimpleNamespace(_runner=runner, _wire="proxy")

    lazy_affine = _shell(_runtime.AffineRunner(lambda: object()))
    try:
        _runtime.unwrap_arg(lazy_affine)
        raise AssertionError("unconstructed affine must refuse cross-thread unwrap")
    except TypeError:
        pass

    # Its first call constructs on its OWN thread; afterwards the
    # wrapper unwraps fine from anywhere.
    try:
        await lazy_affine._runner.call("noop", [])
        raise AssertionError("expected probe method error")
    except InternalError:
        pass
    assert _runtime.unwrap_arg(lazy_affine) is not None
    assert lazy_affine._runner.born_thread_name.startswith("huggorm-affine")

    # Pool runners keep constructing lazily from any thread.
    pool_shell = _shell(_runtime.PoolRunner(lambda: {"ok": True}))
    assert _runtime.unwrap_arg(pool_shell) == {"ok": True}

    # Every emitted wrapper __init__ must state its declared constructor
    # parameters, and pass every one through unwrap_arg.
    #
    # This replaces the old kwargs guard. That one checked a **kwargs
    # forward replayed through unwrap_arg, because a wrapper passed as a
    # keyword argument would otherwise hand the async shell to the sync
    # constructor. Typed parameters make that hazard structurally
    # impossible - there is no **kwargs to forward - so the check moves
    # to what can still go wrong: a parameter the emitter forgot to
    # unwrap, or a signature that drifted from the declaration.
    checked_ctors = 0
    abstract, not_wrapped = [], []
    for c in model.constructed:
        cls_name = c.name
        py = pkg_dir / f"async_{cls_name.lower()}.py"
        if c.wire != "proxy":
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
            # A class with no door has no constructor to check - it has
            # one that refuses. Pin the refusal instead.
            #
            # Keyed on the DOOR, not on `abstract`. That split landed
            # in 061: nix::Store now states the true C++ fact about
            # itself AND keeps its factory, so `abstract` here would
            # have demanded a refusal from the one class that must not
            # refuse.
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
        # Optional parameters must actually be optional.
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

    # Affine service: everything pinned to one dedicated thread.
    remote = AsyncEvalState(AsyncStore("dummy://"))
    assert await remote.get_store_uri() == "dummy://"
    await remote.make_int(1)
    await remote.eval_expr("2")
    assert len(remote._runner.workers_seen) == 1, "affine calls must share one thread"

    # Returned affine value pins to the PRODUCER's thread.
    drv = await remote.make_int(11)
    assert await drv.integer() == 11
    assert await drv.type_name() == "int"
    assert drv._runner.workers_seen == remote._runner.workers_seen, (
        "value ops must run on the producer's thread"
    )

    # Returned pool values are free to use any thread.
    spool = await local.add_to_store("x", b"y", CA.NAR, HashAlgorithm.SHA256)
    assert isinstance(spool, StorePath)
    assert isinstance(drv, AsyncValue)

    # Wire policy lands in the model and on generated classes:
    # immutable types are wire-values, everything else proxies.
    cls = model.classes
    assert cls["StorePath"].wire == "value"
    assert cls["Value"].wire == "proxy"
    assert cls["PathInfo"].wire == "value"
    assert cls["EvalState"].wire == "proxy"
    assert local._wire == "proxy" and spool._wire == "value"

    # Wrapping is a SEPARATE axis from wire policy, and the rule is:
    # wrap when the object needs a home thread (affine) or its methods
    # can block. StorePath and PathInfo are pool and declare
    # _blocking = False, so they cross every layer as themselves -
    # no await in front of a substring read (huggorm#25).
    import huggorm_generated as flg_names
    for name in ("StorePath", "PathInfo"):
        assert not cls[name].decl.blocking and not cls[name].wrapped, name
        assert not hasattr(flg_names, f"Async{name}"), (
            f"Async{name} must not be generated")
        # ...and nothing remote either: with no handle to address, an
        # rpc on it could never be called. It crosses as a value.
        assert not cls[name].served, name
    # The control: an affine class and a blocking pool class both stay
    # wrapped, so the rule is doing work rather than switching nothing.
    assert cls["Value"].wrapped is True
    assert cls["Store"].wrapped is True

    # C++ exceptions surface as InternalError with the cause attached.
    try:
        await remote.parse_expr("")
        raise AssertionError("expected InternalError for an empty expression")
    except InternalError as e:
        d = e.to_dict()
        assert d["code"] == "internal" and d["cause_type"] == "ValueError"

    # Evaluation: EvalState is the affine SERVICE exemplar. Its values
    # attach to its thread, and forcing mutates them in place.
    state = AsyncEvalState(AsyncStore("dummy://"))
    assert await state.get_store_uri() == "dummy://"

    # Thunk protocol: parse gives an unforced value; accessors throw
    # Nix's own type error until it is forced.
    thunk = await state.parse_expr("42")
    assert await thunk.type_name() == "thunk"
    try:
        await thunk.integer()
        raise AssertionError("expected unforced access to fail")
    except NixTypeError as e:
        assert "expected an integer but found a thunk" in str(e)
    await state.force(thunk)
    assert await thunk.type_name() == "int"
    assert await thunk.integer() == 42
    assert await thunk.integer() == 42  # force is idempotent

    # eval returns a fully forced value on the state's thread.
    v = await state.eval_expr('"hello nix"')
    assert isinstance(v, AsyncValue)
    assert await v.string_value() == "hello nix"

    # Free functions have generated wrappers too: module-level
    # coroutines on the shared pool.
    #
    # NONE OF THEM TAKES A BOUND HANDLE. `describe(obj: MockStore)`
    # was the only one, and it went with the mock (huggorm#60), so the
    # emitter's parameter-unwrapping path for a free function has no
    # user until nix::copyPaths or the libexpr EvalState brings one
    # back. Said here rather than left as a silent hole.
    import huggorm_generated as flg
    from huggorm_bindings.errors import UnimplementedError

    # A Nix built without the collector refuses, by name, every question
    # only the collector can answer, and the checks that need one run
    # where it exists (huggorm#105).
    has_gc = huggorm_bindings.boehm_gc()

    async def collect() -> None:
        if has_gc:
            await flg.collect_garbage()

    # An untouched affine wrapper constructs on its OWN thread, on the
    # first call. ensure() refuses to build a dedicated-thread object
    # off-home, rightly, and the runner satisfies that refusal rather
    # than relaxing it.
    #
    # It read `untouched.force(thunk_arg)` with a thunk from `state`,
    # which is a CROSS-STATE call and now refused: a state is an
    # isolation, and a value is only meaningful to the state that
    # allocated it. The cross-state part was scaffolding - what is
    # asserted is where `untouched` was born, and any call proves that.
    #
    # The comment there claimed this covered an affine wrapper used as
    # an ARGUMENT, and it never did. `_materialize_args` was a no-op on
    # `thunk_arg`, which is attached to an already-constructed
    # producer - and that is true of EVERY affine argument the corpus
    # can produce, because the only ones are Values and a Value comes
    # from a state that has by then been called. So the argument side
    # of `materialize` has no producer to exercise it, which is said
    # here rather than left looking covered.
    untouched = AsyncEvalState(AsyncStore("dummy://"))
    assert untouched._runner._obj is None, "expected an unconstructed wrapper"
    assert await (await untouched.eval_expr("1")).integer() == 1
    born = untouched._runner.born_thread_name
    assert born is not None and born.startswith("huggorm-affine"), (
        f"argument construction must stay on its own thread, not {born}")
    await untouched.aclose()
    free = model.functions
    assert free["collect_garbage"].returns is None
    # ...and the wire can carry each. gc_stats returns dict[str, int],
    # which is a protobuf map now that the declaration says what the
    # entries hold (huggorm#30).
    assert not model.function_blockers(free["collect_garbage"])
    assert not model.function_blockers(free["gc_stats"])
    gc_return = free["gc_stats"].returns
    assert gc_return is not None and gc_return.spelling == "dict[str, int]"

    # gc_stats was the last function with no RPC surface, so the
    # blocker path now has nothing left to report. Exercise it
    # directly, or the mechanism that keeps an unrepresentable type out
    # of the schema goes untested the moment everything is
    # representable.
    from huggorm_gen import ir

    T = ir.TypeRef
    i, s = T.named("int", "scalar"), T.named("str", "scalar")
    value, path = T.named("Value", "proxy"), T.named("StorePath", "value")
    served = frozenset({"Value"})
    # A container of PROXIES stays refused whichever container it is:
    # one lease per element is not something anything grants in bulk.
    # Neither container nests in the other - proto3 has no repeated map
    # field and no map of repeated values. An opaque object and a
    # module type with no wire spelling have no field at all. The
    # reader refuses the rest before a model exists: a set, a
    # non-str key, a bare container, a name nothing declares.
    for t in (T.dict_of(T.dict_of(i)), T.dict_of(T.list_of(i)),
              T.dict_of(value), T.list_of(value), T.list_of(T.list_of(i)),
              T.list_of(T.dict_of(i)), T.named("object", "opaque"),
              T.named("pathlib.Path", "module")):
        assert ir.wire_blocker(t, served), f"{t.spelling} should be blocked"
    for t in (s, i, value, path, T.dict_of(i), T.dict_of(path),
              T.list_of(i), T.list_of(path),
              T.named("datetime.timedelta", "module")):
        assert not ir.wire_blocker(t, served), (t.spelling,
                                                ir.wire_blocker(t, served))

    # Closing an affine wrapper shuts its dedicated thread down, and
    # that thread must leave the collector's list before it dies. Boehm
    # stops the world by signalling every registered thread and waiting
    # for each to answer; a dead one never answers, so the next
    # collection aborted the PROCESS with "Signals delivery fails
    # constantly". Nothing caught it because every existing aclose
    # happened after the last collection. Two affine wrappers were
    # closed just above, so collect here.
    await collect()
    if has_gc:
        assert huggorm_bindings.gc_stats()["heap_size"] > 0
    else:
        for refused in (huggorm_bindings.gc_stats, huggorm_bindings.collect_garbage):
            try:
                refused()
            except UnimplementedError:
                continue
            raise AssertionError(f"{refused.__name__} answered with no collector")

    # ---- collections ------------------------------------------------
    # An attribute set is BUILT, not parsed: the expression language
    # stays a toy, and reimplementing Nix's syntax would buy nothing
    # the wire and lifetime paths do not get from a builder.
    builder = AsyncEvalState(AsyncStore("dummy://"))
    attrs = await builder.make_attrs()
    for name, number in (("zebra", 1), ("apple", 2), ("mango", 3)):
        await builder.attrs_set(attrs, name, await builder.make_int(number))
    assert await attrs.type_name() == "attrs"
    assert await attrs.size() == 3

    # Nix attribute sets are alphabetical, so an index walk IS the
    # listing order (Carl, 2026-08-25). The C++ side keeps a sorted
    # array, like nix::Bindings.
    names = [await attrs.name_at(i) for i in range(await attrs.size())]
    assert names == ["apple", "mango", "zebra"], names
    assert [await (await attrs.value_at(i)).integer() for i in range(3)] == [2, 3, 1]
    assert await attrs.has("mango") and not await attrs.has("durian")
    assert await (await attrs.get("apple")).integer() == 2

    # Setting a name twice replaces its value, like assignment.
    await builder.attrs_set(attrs, "apple", await builder.make_int(99))
    assert await attrs.size() == 3
    assert await (await attrs.get("apple")).integer() == 99

    # Nesting: a list inside an attribute set, holding values that are
    # values in their own right.
    xs = await builder.make_list()
    for word in ("one", "two"):
        await builder.list_append(xs, await builder.make_string(word))
    await builder.attrs_set(attrs, "xs", xs)
    assert await (await (await attrs.get("xs")).at(1)).string_value() == "two"

    # The collector must SEE the children through the parent. A plain
    # std::vector<Value *> inside a GC-allocated Value would hold them
    # in malloc memory, which Boehm does not scan: they would be
    # collected while the parent still pointed at them. Drop every
    # Python reference, collect, then churn hard enough that a freed
    # block would be handed out again - and read the tree back.
    del xs
    gc.collect()
    await collect()
    churn = [await builder.make_int(i) for i in range(500)]
    del churn
    gc.collect()
    await collect()
    assert await attrs.size() == 4
    assert await (await attrs.get("apple")).integer() == 99
    assert await (await (await attrs.get("xs")).at(0)).string_value() == "one"
    assert [await attrs.name_at(i) for i in range(4)] == [
        "apple", "mango", "xs", "zebra"]

    # Wrong-kind and out-of-range access say which, on every accessor.
    # Built one at a time: a tuple of coroutines leaves the untried ones
    # unawaited the moment the first raises.
    for make in (lambda: attrs.integer(),
                 lambda: attrs.at(0),
                 lambda: attrs.name_at(99)):
        try:
            await make()
        except Exception as e:
            # A wrong kind is Nix's own type error. The runtime wraps
            # another binding failure in InternalError, so its C++ text
            # is on the cause, not the message.
            why = str(e.__cause__ or e)
            assert "but found" in why or "out of range" in why, why
        else:
            raise AssertionError("wrong-kind access succeeded")
    await builder.aclose()

    # THE HIERARCHY IS GONE, AND SO ARE THE THREE PROPERTIES IT WAS
    # THE ONLY EXERCISE FOR (huggorm#60):
    #
    # - a generated base whose subclasses share one wire service;
    # - the pool policy DROPPING an affine-returning method from a
    #   pool wrapper while an affine wrapper keeps it;
    # - an abstract base refusing construction.
    #
    # Real Nix has the hierarchy - nix::Store over nix::LocalStore and
    # the rest - but nanobind downcasts by EXACT typeid, so registering
    # an intermediate buys nothing and the leaves are a zoo this repo
    # does not track. Carl has no use case for the downcast either.
    # These come back if and when a second real base does.

    # Boehm GC proof, in two layers. First the counters bound straight
    # from gc.h prove the collector is ACTIVE and that this exact value
    # lives inside a GC-allocated block. A no-op integration could not
    # produce either fact.
    if has_gc:
        stats = huggorm_bindings.gc_stats()
        assert stats["heap_size"] > 0 and stats["total_bytes"] > 0
        assert await v.is_gc_managed()
        assert await thunk.is_gc_managed()

        # Second layer: survival. Collection is a blocking global operation,
        # so it is dispatched off the loop thread - which also exercises
        # thread registration from a fresh pool thread.
        collections_before = stats["collections"]
        await collect()
        assert huggorm_bindings.gc_stats()["collections"] >= collections_before + 2
        assert await v.string_value() == "hello nix"
        await collect()
    else:
        assert not await v.is_gc_managed(), "nothing is GC-managed with no collector"
    # Forced state persists through collection...
    assert await thunk.type_name() == "int"
    assert await thunk.integer() == 42
    # ...and the arena keeps accepting new values afterwards.
    fresh = await state.parse_expr("7")
    assert await fresh.type_name() == "thunk"
    await state.force(fresh)
    assert await fresh.integer() == 7

    # Every value that is made releases its root when it is dropped.
    #
    # OUR invariant, deliberately, and it replaces a heap-bytes
    # assertion that was asking the wrong party. Two things made that
    # one unsound. An evaluated value is rooted by the STATE - the
    # file cache, the env chain - so dropping the Python handle
    # removes our root and boehm rightly keeps the value. And boehm is
    # conservative: a stale pointer in a register legitimately retains
    # an object, so a byte count is flaky by construction.
    #
    # A leaked root is a real bug class and nothing else can see it.
    # It keeps its value alive forever, and the heap only ever says
    # the heap grew.
    if has_gc:
        roots_before = huggorm_bindings.gc_stats()["live_roots"]
        kept = [await state.make_string(f'{"p" * 200}-{i}') for i in range(200)]
        assert huggorm_bindings.gc_stats()["live_roots"] >= roots_before + 200

        del kept
        await collect()
        assert huggorm_bindings.gc_stats()["live_roots"] == roots_before, (
            f"dropped values must release their roots: "
            f"{roots_before} -> {huggorm_bindings.gc_stats()['live_roots']}"
        )

    assert v._runner.workers_seen == state._runner.workers_seen, (
        "value ops must run on the producer's thread"
    )

    # Affine serialization, proved by WHERE the calls ran rather than
    # by how long they took.
    #
    # This used to gather two evals and require the elapsed time to be
    # about twice one eval. That worked because the mock SLEPT: real
    # libexpr evaluates "1" in microseconds, so a timing test measures
    # scheduler noise and passes or fails on the machine's mood. A
    # single worker is the property the policy actually promises, and
    # a thread name is not a stopwatch.
    await _all(state.eval_expr("1"), state.eval_expr("2"))
    assert len(state._runner.workers_seen) == 1, (
        f"evals must serialize on one thread, saw {state._runner.workers_seen}")

    # Evaluation errors, in the two shapes a caller has to tell apart.
    #
    # A nix::Error is DECLARED, so it reaches the caller as itself and
    # `except NixError` works here exactly as it does against the sync
    # binding (huggorm#66). It describes itself through `to_dict`, which
    # is what the runtime tests before deciding to wrap anything.
    #
    # Anything else is not declared, carries no parts, and arrives as
    # an InternalError naming it. The mock could only ever raise the
    # second kind, so this pairing is new.
    # The error module by its DERIVED name. `huggorm_bindings` is a
    # fixed fact in this file, but the errors submodule is named after
    # the declaration - so writing `.errors` here would have been a
    # copy of a name the build computes, and renaming the declaration
    # proved it (huggorm#63).
    from huggorm_generated._policy import ERROR_MODULE

    NixError = importlib.import_module(ERROR_MODULE).NixError

    try:
        await state.eval_expr("not an expression")
        raise AssertionError("expected an evaluation error")
    except NixError as e:
        # The narrowest declared class: an undefined variable is a
        # nix::UndefinedVarError, and `except NixError` still catches it.
        d = e.to_dict()
        assert d["code"] == "UndefinedVarError", d
        assert "undefined variable" in d["message"], d
    try:
        await state.eval_expr("")
        raise AssertionError("expected a refusal")
    except InternalError as e:
        d = e.to_dict()
        assert d["code"] == "internal" and d["cause_type"] == "ValueError", d

    await v.aclose()
    await thunk.aclose()
    await state.aclose()

    # Wrong arity fails AT THE CALL SITE now. It used to sail through
    # __init__(*args, **kwargs) and surface as an InternalError raised
    # from inside the lazy factory, on a worker thread, at the first
    # method call - far from the line that caused it.
    #
    # Caching of a GENUINE factory failure is covered directly above,
    # via PoolRunner(bad_factory); that guarantee is unchanged.
    try:
        AsyncEvalState(AsyncStore("dummy://"), None, None, "unexpected-arg")  # type: ignore[call-arg]
        raise AssertionError("wrong arity must fail at construction")
    except TypeError:
        pass

    # A declared required parameter is required, and a declared optional
    # one is optional.
    try:
        AsyncEvalState()  # type: ignore[call-arg]
        raise AssertionError("missing required store must fail")
    except TypeError:
        pass
    await drv.aclose()
    await remote.aclose()
    await local.aclose()


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
    manifest string and an emitted node compare as source."""
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
    twins = dict(model.twins)
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
                    expected = twins.get(expected, expected)
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
        if rt is not None and rt.origin in ("", "optional") \
                and rt.kind == "proxy":
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

    Reflected against the LIVE modules, not the manifest. The manifest
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
    _UNIONS.update({n: [a.name for a in arms] for n, arms in model.unions.items()})
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
                      + (" | None" if p.default == "None"
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
    declaration typechecks. The stub says `str`, the manifest says
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
    #
    # Definition order is the run order - a module's dict keeps it -
    # and the sync checks go first so the one async check still runs
    # last, which is the only ordering the old list expressed on
    # purpose.
    checks = [fn for name, fn in list(globals().items())
              if name.startswith("test_") and callable(fn)]

    def call(fn: Any) -> Any:
        # Every check takes the output directory or nothing, and the
        # signature says which - so adding one needs no registration.
        return fn(out) if inspect.signature(fn).parameters else fn()

    for fn in checks:
        if not inspect.iscoroutinefunction(fn):
            call(fn)
    for fn in checks:
        if inspect.iscoroutinefunction(fn):
            # `anyio.run` takes the function and its arguments, where
            # `asyncio.run` took the coroutine. `call` decides the
            # arguments from the signature, so a lambda is what hands
            # anyio something to call.
            anyio.run(lambda f=fn: call(f))  # type: ignore[arg-type,misc]
    print(f"smoke test OK ({len(checks)} checks)")


if __name__ == "__main__":
    main()
