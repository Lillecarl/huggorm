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
import asyncio
import gc
import importlib
import inspect
import pathlib
import re
import sys
from typing import Any


def test_parse(out: pathlib.Path) -> None:
    for py in sorted(out.glob("*.py")):
        ast.parse(py.read_text(), filename=str(py))


def test_annotation_rendering() -> None:
    """A subscripted generic renders in full, wherever it is written.

    `__name__` on one answers with the head, so `dict[str, int]` became
    `dict` - and whether it did depended on which path resolved the
    annotation. A free function's stayed the written string and kept
    its parameters; a method's resolved to a real generic and lost
    them. The same declaration meant two different things depending on
    where it appeared."""
    from huggorm_gen.pygen.model import _annotation_name, check_collection_contract

    assert _annotation_name(dict[str, int]) == "dict[str, int]"
    assert _annotation_name(list[str]) == "list[str]"
    assert _annotation_name(dict[str, list[int]]) == "dict[str, list[int]]"
    assert _annotation_name(int) == "int"
    assert _annotation_name(None) == "None"

    # ...which is what lets the collection rule see inside one. A
    # container of wrapped types builds, emits a schema, and then hands
    # back bare sync objects: nothing attaches a runner to elements.
    protos = [{
        "name": "V", "wrapped": True, "methods": [
            {"name": "attrs", "return_type": "dict[str, V]"},
            {"name": "items", "return_type": "list[V]"},
            {"name": "one", "return_type": "V"},
            {"name": "n", "return_type": "int"},
        ]}]
    bad = check_collection_contract(protos)
    assert len(bad) == 2, bad
    assert all("attrs" in b or "items" in b for b in bad), bad


def test_a_resolved_class_keeps_its_module() -> None:
    """An annotation must not depend on its NEIGHBOURS.

    get_type_hints resolves a whole function at once. A method with a
    `StorePath` parameter can fail to resolve - the name is not a
    Python global - so its written strings survive; one whose
    annotations all resolve gets real classes, and `__name__` on a
    class drops the module it lives in. So `-> pathlib.Path` meant
    `pathlib.Path` or `Path` depending on what else the method
    declared, and the second emits a NameError.

    The module HEAD is what gets written, because that is what an
    author writes and what a reader can import - checked, not assumed,
    so a class the head does not re-export gets its full path."""
    import collections.abc
    import datetime
    import pathlib

    from huggorm_gen.pygen.model import _annotation_name

    assert _annotation_name(pathlib.Path) == "pathlib.Path"
    assert _annotation_name(datetime.datetime) == "datetime.datetime"

    # A builtin stays bare, and so does a binding class: every emitted
    # module imports those from the package root.
    from huggorm_bindings import StorePath

    assert _annotation_name(str) == "str"
    assert _annotation_name(StorePath) == "StorePath"
    assert _annotation_name(dict[str, int]) == "dict[str, int]"

    # collections.Sequence has not existed since 3.10, so the head is
    # the wrong answer here and the full path is the right one.
    assert getattr(collections, "Sequence", None) is None
    assert (_annotation_name(collections.abc.Sequence)
            == "collections.abc.Sequence")


def test_a_default_is_written_or_refused() -> None:
    """What a parameter default may be, and what happens to the rest.

    The generated surfaces WRITE the default as source, so a default
    this cannot write has to stop the build - a wrapper missing one
    the binding has would answer a short call differently depending on
    where the object lives.

    An enum member is written as the member. A StrEnum member IS a
    string, so `repr` gives `'nar'`, which calls correctly and which a
    typechecker rejects: a str is not a ContentAddressMethod."""
    import enum
    import inspect

    from huggorm_gen.pygen.model import default_source

    class Word(enum.StrEnum):
        NAR = "nar"

    def src(value: object, type_str: str = "str") -> str | None:
        return default_source(value, type_str, "probe:p")

    assert src(inspect.Parameter.empty) is None
    assert src(Word.NAR) == "Word.NAR"
    assert src("nar") == "'nar'"
    assert src(7) == "7" and src(True) == "True" and src(b"x") == "b'x'"

    # None depends on the TYPE. A repeated protobuf field has no
    # presence problem - an absent one and an empty one are the same
    # field - so a container may default to None and nothing else may.
    assert src(None, "list[StorePath]") == "None"
    assert src(None, "dict[str, int]") == "None"

    # `[]` is what a reader expects instead, and it is worse: a mutable
    # default, once per generated surface.
    for bad, type_str, why in (
            (None, "str", "not a container"),
            (None, "StorePath", "not a container"),
            ([], "list[StorePath]", "mutable default"),
            (float("inf"), "str", "not a literal"),
            (object(), "str", "not a literal")):
        try:
            src(bad, type_str)
        except ValueError as e:
            assert why in str(e), (bad, str(e))
        else:
            raise AssertionError(f"{bad!r} was accepted as a default")


def test_an_enum_is_a_scalar_everywhere() -> None:
    """One rule, held by all three layers that had an opinion.

    The schema always accepted an enum wherever a scalar goes -
    alone, in a list, in a map - because a StrEnum member IS a string.
    check_wire_contract did not: it tested a field type against the
    primitives and the class protos, and an enum is in neither, so a
    _wire_fields entry of enum type failed the build as an unknown
    type while the rpc layer accepted the same declaration.

    The codec half of this is tested where the codec lives; here is
    the half the generator decides."""
    from huggorm_gen.pygen.grpc_schema import wire_blocker
    from huggorm_gen.pygen.model import check_wire_contract

    kinds = {"StorePath": "value", "Word": "enum"}
    for spelling in ("Word", "list[Word]", "dict[str, Word]"):
        assert wire_blocker(spelling, kinds) is None, spelling

    def proto(ftype: str) -> dict[str, object]:
        return {"name": "Probe", "wire": "value", "threading": "pool",
                "wire_fields": [["kind", ftype]],
                "_helpers": ["_from_parts", "_parts"],
                "dunders": ["__eq__", "__hash__", "__repr__"]}

    for ftype in ("Word", "list[Word]"):
        assert check_wire_contract([proto(ftype)], {"Word"}) == [], ftype
    # ...and a name that is neither a class nor a declared enum still
    # fails, so the set widened rather than the check weakening.
    complaints = check_wire_contract([proto("Nonsense")], {"Word"})
    assert len(complaints) == 1 and "unknown field type" in complaints[0]


def test_an_optional_return_names_a_value_or_nothing() -> None:
    """`T | None` is a real return type, and only for some T.

    Absence rides on presence, which is one bit. So it separates ONE
    type from nothing: a union of two real types has no field to put
    either arm in.

    A scalar is allowed, and used not to be. proto3 has had explicit
    `optional` since 3.15 - a synthetic oneof gives a scalar field
    real presence - so the old refusal described what this schema
    builder emitted rather than what proto3 can say (tasks/048).

    The refusal that matters most is a WRAPPED T. The wire could
    almost carry it - a Handle is a message - but every layer above
    adopts a returned proxy into a runner, and none of them adopts
    nothing. That one is refused for every surface at once rather than
    only for the wire."""
    from huggorm_gen.payload.wiretypes import optional_value
    from huggorm_gen.pygen.grpc_schema import wire_blocker
    from huggorm_gen.pygen.model import check_optional_contract

    assert optional_value("StorePath | None") == "StorePath"
    assert optional_value("None | StorePath") == "StorePath"
    assert optional_value("StorePath") is None
    assert optional_value("list[StorePath]") is None

    for bad in ("str | int", "str | int | None"):
        try:
            optional_value(bad)
        except TypeError as e:
            assert "no wire representation" in str(e), (bad, str(e))
        else:
            raise AssertionError(f"{bad!r} was accepted as an optional")

    kinds = {"StorePath": "value", "Store": "proxy", "Word": "enum"}
    for good in ("StorePath | None", "str | None", "int | None",
                 "Word | None"):
        assert wire_blocker(good, kinds) is None, good
    for bad, why in (("list[StorePath] | None", "IS an empty one"),
                     ("Store | None", "adopts nothing")):
        blocker = wire_blocker(bad, kinds)
        assert blocker is not None and why in blocker, (bad, blocker)

    def proto(rt: str) -> dict[str, object]:
        return {"name": "Probe", "wrapped": True, "threading": "pool",
                "methods": [{"name": "find", "return_type": rt, "params": []}]}

    assert check_optional_contract([proto("StorePath | None")]) == []
    complaints = check_optional_contract([proto("Probe | None")])
    assert len(complaints) == 1 and "adopts nothing" in complaints[0]


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


async def test_behavior() -> None:
    import tempfile

    import huggorm_bindings
    from huggorm_bindings import ContentAddressMethod as CA
    from huggorm_bindings import HashAlgorithm, StorePath
    from huggorm_generated import (
        AsyncEvalState,
        AsyncStore,
        AsyncValue,
    )
    from huggorm_generated._runtime import InternalError

    def added(name: str, body: bytes) -> tuple[str, bytes]:
        return name, body

    from huggorm_gen.pygen.generate import build_manifest

    manifest = build_manifest()
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
    t0 = asyncio.get_running_loop().time()
    p1, p2 = await asyncio.gather(
        local.add_to_store("hello.txt", b"world", CA.NAR, HashAlgorithm.SHA256),
        local.add_to_store("note.txt", b"nix real", CA.NAR, HashAlgorithm.SHA256),
    )
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 0.18, f"expected overlapped adds, took {elapsed:.2f}s"
    # StorePath is pool AND non-blocking, so it has no wrapper: an
    # awaited store method hands back the binding object itself, and
    # reading it is a plain call (tasks/025).
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
    results = await asyncio.gather(*(runner.call("noop", []) for _ in range(8)))
    assert results == ["ok"] * 8
    assert len(made) == 1, f"factory ran {len(made)}x under concurrent first-calls"

    # Same guarantee on the failure path: one attempt, every caller
    # gets the cached error.
    failed: list[int] = []

    def bad_factory() -> object:
        failed.append(1)
        raise RuntimeError("no")

    bad_runner = _runtime.PoolRunner(bad_factory)
    errs = await asyncio.gather(
        *(bad_runner.call("noop", []) for _ in range(4)), return_exceptions=True
    )
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
    # unwrap, or a signature that drifted from the manifest.
    checked_ctors = 0
    abstract, not_wrapped = [], []
    for cls_name, proto in manifest["wrappers"].items():
        py = pkg_dir / f"async_{cls_name.lower()}.py"
        if not proto["wrapped"]:
            # No wrapper was emitted, so there is no emitted __init__
            # to check. What must hold instead is that nothing was
            # emitted at all.
            assert not py.exists(), (
                f"{cls_name} is not wrapped, yet {py.name} exists")
            not_wrapped.append(cls_name)
            continue
        tree = ast.parse(py.read_text(), filename=str(py.name))
        init = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        )
        if not proto["constructs"]:
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
        declared = [p["name"] for p in proto["ctor"]]
        emitted = [a.arg for a in init.args.args[1:]]  # drop self
        assert emitted == declared, (
            f"{py.name}.__init__ takes {emitted}, manifest declares {declared}"
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
        n_optional = sum(1 for p in proto["ctor"] if p["default"] is not None)
        assert len(init.args.defaults) == n_optional, (
            f"{py.name}.__init__ has {len(init.args.defaults)} default(s), "
            f"manifest declares {n_optional} optional parameter(s)"
        )
        checked_ctors += 1
    expected = len(manifest["wrappers"]) - len(abstract) - len(not_wrapped)
    assert checked_ctors == expected, (
        f"checked {checked_ctors} wrapper ctors, expected {expected} "
        f"(abstract, so skipped: {abstract}; unwrapped: {not_wrapped})"
    )
    assert not_wrapped, "expected at least one unwrapped class in the surface"

    # Affine service: everything pinned to one dedicated thread.
    remote = AsyncEvalState("dummy://")
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

    # Wire policy lands in the manifest (the future RPC IDL) and on
    # generated classes: immutable types are wire-values, everything
    # else proxies.
    def proto_of(name: str) -> dict:
        """One class's manifest entry, whichever group holds it.

        Which group a class lands in is a fact about how it is made -
        `@produced(by=...)` moves one - and naming the group here
        would restate that, so a rename of the producer breaks a test
        about wire policy. Ask for the name.
        """
        for group in ("wrappers", "returned_types"):
            if name in manifest[group]:
                return manifest[group][name]
        raise AssertionError(f"{name} is in no manifest group")

    assert proto_of("StorePath")["wire"] == "value"
    assert proto_of("Value")["wire"] == "proxy"
    assert proto_of("PathInfo")["wire"] == "value"
    assert proto_of("EvalState")["wire"] == "proxy"
    assert local._wire == "proxy" and spool._wire == "value"

    # Wrapping is a SEPARATE axis from wire policy, and the rule is:
    # wrap when the object needs a home thread (affine) or its methods
    # can block. StorePath and PathInfo are pool and declare
    # _blocking = False, so they cross every layer as themselves -
    # no await in front of a substring read (tasks/025).
    import huggorm_generated as flg_names
    for name in ("StorePath", "PathInfo"):
        proto = proto_of(name)
        assert proto["blocking"] is False and proto["wrapped"] is False, proto
        assert not hasattr(flg_names, f"Async{name}"), (
            f"Async{name} must not be generated")
        # ...and nothing remote either: with no handle to address, an
        # rpc on it could never be called.
        assert "service" not in proto and "acquire" not in proto, proto
        assert all("rpc" not in m for m in proto["methods"]), proto["methods"]
        # It still has a wire message: it crosses as a value.
        assert proto["message"], proto
    # The control: an affine class and a blocking pool class both stay
    # wrapped, so the rule is doing work rather than switching nothing.
    assert proto_of("Value")["wrapped"] is True
    assert proto_of("Store")["wrapped"] is True

    # C++ exceptions surface as InternalError with the cause attached.
    try:
        await remote.parse_expr("")
        raise AssertionError("expected InternalError for an empty expression")
    except InternalError as e:
        d = e.to_dict()
        assert d["code"] == "internal" and d["cause_type"] == "ValueError"

    # Evaluation: EvalState is the affine SERVICE exemplar. Its values
    # attach to its thread, and forcing mutates them in place.
    state = AsyncEvalState("dummy://")
    assert await state.get_store_uri() == "dummy://"

    # Thunk protocol: parse gives an unforced value; accessors throw
    # until it is forced.
    thunk = await state.parse_expr("42")
    assert await thunk.type_name() == "thunk"
    try:
        await thunk.integer()
        raise AssertionError("expected unforced access to fail")
    except InternalError as e:
        assert type(e.__cause__) is RuntimeError
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
    # was the only one, and it went with the mock (tasks/060), so the
    # emitter's parameter-unwrapping path for a free function has no
    # user until nix::copyPaths or the libexpr EvalState brings one
    # back. Said here rather than left as a silent hole.
    import huggorm_generated as flg

    # An affine wrapper is usable as an argument from the FIRST call.
    # It used to depend on call order: ensure() refuses to build a
    # dedicated-thread object off-home (rightly), so a state nobody had
    # touched yet could not be passed anywhere. Every wrapper above had
    # been called already, which is why this went unseen. The runner
    # now constructs on its own thread before the argument is unwrapped.
    untouched = AsyncEvalState("dummy://")
    assert untouched._runner._obj is None, "expected an unconstructed wrapper"
    thunk_arg = await state.parse_expr("1")
    await untouched.force(thunk_arg)
    assert await thunk_arg.integer() == 1
    born = untouched._runner.born_thread_name
    assert born is not None and born.startswith("huggorm-affine"), (
        f"argument construction must stay on its own thread, not {born}")
    await untouched.aclose()
    free = manifest["free_functions"]
    assert free["collect_garbage"]["return_type"] == "None"
    # ...and each one records whether the wire can carry it, with the
    # reason when it cannot. gc_stats returns dict[str, int], which is
    # a protobuf map now that the declaration says what the entries
    # hold (tasks/030).
    assert not free["collect_garbage"]["wire_blockers"]
    assert not free["gc_stats"]["wire_blockers"], free["gc_stats"]["wire_blockers"]
    assert free["gc_stats"]["return_type"] == "dict[str, int]"
    assert "rpc" in free["gc_stats"]

    # gc_stats was the last function with no RPC surface, so the
    # blocker path now has nothing left to report. Exercise it
    # directly, or the mechanism that keeps an unrepresentable type out
    # of the schema goes untested the moment everything is
    # representable.
    from huggorm_gen.pygen.grpc_schema import wire_blocker

    kinds = {"Value": "proxy", "StorePath": "value"}
    # A container of PROXIES stays refused whichever container it is:
    # one lease per element is not something anything grants in bulk.
    # Neither container nests in the other - proto3 has no repeated map
    # field and no map of repeated values - and a bare one says nothing
    # about what it holds. set and tuple have no field at all.
    for shape in ("dict", "dict[int, str]", "dict[str, dict[str, int]]",
                  "dict[str, list[int]]", "dict[str, Value]",
                  "list", "list[Value]", "list[list[int]]",
                  "list[dict[str, int]]", "set[int]", "Nowhere"):
        assert wire_blocker(shape, kinds), f"{shape} should be blocked"
    for shape in ("str", "int", "Value", "StorePath", "dict[str, int]",
                  "dict[str, StorePath]", "list[int]",
                  "list[StorePath]"):
        assert not wire_blocker(shape, kinds), (shape, wire_blocker(shape, kinds))

    # Closing an affine wrapper shuts its dedicated thread down, and
    # that thread must leave the collector's list before it dies. Boehm
    # stops the world by signalling every registered thread and waiting
    # for each to answer; a dead one never answers, so the next
    # collection aborted the PROCESS with "Signals delivery fails
    # constantly". Nothing caught it because every existing aclose
    # happened after the last collection. Two affine wrappers were
    # closed just above, so collect here.
    await flg.collect_garbage()
    assert huggorm_bindings.gc_stats()["heap_size"] > 0

    # ---- collections ------------------------------------------------
    # An attribute set is BUILT, not parsed: the expression language
    # stays a toy, and reimplementing Nix's syntax would buy nothing
    # the wire and lifetime paths do not get from a builder.
    builder = AsyncEvalState("dummy://")
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
    await flg.collect_garbage()
    churn = [await builder.make_int(i) for i in range(500)]
    del churn
    gc.collect()
    await flg.collect_garbage()
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
            # The runtime wraps a binding failure in InternalError, so
            # the C++ text is on the cause, not the message.
            why = str(e.__cause__ or e)
            assert "is not" in why or "out of range" in why, why
        else:
            raise AssertionError("wrong-kind access succeeded")
    await builder.aclose()

    # THE HIERARCHY IS GONE, AND SO ARE THE THREE PROPERTIES IT WAS
    # THE ONLY EXERCISE FOR (tasks/060):
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
    stats = huggorm_bindings.gc_stats()
    assert stats["heap_size"] > 0 and stats["total_bytes"] > 0
    assert await v.is_gc_managed()
    assert await thunk.is_gc_managed()

    # Second layer: survival. Collection is a blocking global operation,
    # so it is dispatched off the loop thread - which also exercises
    # thread registration from a fresh pool thread.
    collections_before = stats["collections"]
    await flg.collect_garbage()
    assert huggorm_bindings.gc_stats()["collections"] >= collections_before + 2
    assert await v.string_value() == "hello nix"
    await flg.collect_garbage()
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
    roots_before = huggorm_bindings.gc_stats()["live_roots"]
    kept = [await state.make_string(f'{"p" * 200}-{i}') for i in range(200)]
    assert huggorm_bindings.gc_stats()["live_roots"] >= roots_before + 200

    del kept
    await flg.collect_garbage()
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
    await asyncio.gather(state.eval_expr("1"), state.eval_expr("2"))
    assert len(state._runner.workers_seen) == 1, (
        f"evals must serialize on one thread, saw {state._runner.workers_seen}")

    # Evaluation errors, in the two shapes a caller has to tell apart.
    #
    # A nix::Error is DECLARED, so it reaches the caller as itself and
    # `except NixError` works here exactly as it does against the sync
    # binding (tasks/066). It describes itself through `to_dict`, which
    # is what the runtime tests before deciding to wrap anything.
    #
    # Anything else is not declared, carries no parts, and arrives as
    # an InternalError naming it. The mock could only ever raise the
    # second kind, so this pairing is new.
    # The error module by its DERIVED name. `huggorm_bindings` is a
    # fixed fact in this file, but the errors submodule is named after
    # the declaration - so writing `.errors` here would have been a
    # copy of a name the build computes, and renaming the declaration
    # proved it (tasks/063).
    from huggorm_generated._policy import ERROR_MODULE

    NixError = importlib.import_module(ERROR_MODULE).NixError

    try:
        await state.eval_expr("not an expression")
        raise AssertionError("expected an evaluation error")
    except NixError as e:
        d = e.to_dict()
        assert d["code"] == "NixError", d
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
        AsyncEvalState("dummy://", "unexpected-arg")  # type: ignore[call-arg]
        raise AssertionError("wrong arity must fail at construction")
    except TypeError:
        pass

    # A declared required parameter is required, and a declared optional
    # one is optional.
    try:
        AsyncEvalState()  # type: ignore[call-arg]
        raise AssertionError("missing required store_uri must fail")
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
                if f.name.startswith("_"):
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


def test_the_manifest_is_what_got_written(out: pathlib.Path) -> None:
    """The emitted surfaces against the MANIFEST, not each other.

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
    from huggorm_gen.pygen.generate import build_manifest

    manifest = build_manifest()
    wrapped = {
        name: proto
        for group in ("wrappers", "returned_types")
        for name, proto in manifest[group].items()
        if proto["wrapped"]
    }
    found = _emitted_classes(out)

    def declared(name: str | None) -> dict[str, Any]:
        """Every method the chain declares, leaf definitions winning.
        018 splits a surface across a base and its subclasses, so one
        class's own list is only part of what it offers."""
        out_: dict[str, Any] = {}
        while name is not None:
            proto = wrapped[name]
            for m in proto["methods"]:
                out_.setdefault(m["name"], m)
            name = proto.get("async_base")
        return out_

    failures, checked, ctor_checked = [], 0, 0
    for cls_name, proto in wrapped.items():
        emitted = _resolved(found, proto["async_class"])
        for name, m in declared(cls_name).items():
            sig = emitted.get(name)
            if sig is None:
                failures.append(f"{cls_name}.{name}: declared, not emitted")
                continue
            checked += 1
            want_names = [p["name"] for p in m["params"]]
            if sig["params"] != want_names:
                failures.append(
                    f"{cls_name}.{name} takes {sig['params']}, the manifest "
                    f"declares {want_names}")
            # Defaults align to the END of the parameter list, the way
            # Python aligns them.
            want_defaults = [_expr(p["default"]) for p in m["params"]
                             if p["default"] is not None]
            if [_expr(d) for d in sig["defaults"]] != want_defaults:
                failures.append(
                    f"{cls_name}.{name} defaults to {sig['defaults']}, the "
                    f"manifest declares {want_defaults}")

    # ...and the CONSTRUCTORS, read off the stubs. That is where a
    # constructor default actually lands: the only class with one is a
    # returned type, and a returned type's emitted __init__ takes
    # (obj, runner) rather than the declared parameters. A check
    # against the wrappers alone would have compared two empty lists
    # and said nothing.
    from huggorm_gen.pygen.emitter import STUB_PACKAGE

    # Every declared class, not only the wrapped ones: the stubs
    # describe the BINDINGS, which the generated surface has filtered.
    declared_classes = {
        name: pr
        for group in ("wrappers", "returned_types")
        for name, pr in manifest[group].items()
    }
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
            want = [_expr(p["default"])
                    for p in declared_classes[node.name].get("ctor") or []
                    if p["default"] is not None]
            got = [_expr(ast.unparse(d)) for d in init.args.defaults]
            if got != want:
                failures.append(
                    f"{pyi.name}:{node.name}.__init__ defaults to {got}, "
                    f"the manifest declares {want}")
            ctor_checked += 1

    assert not failures, ("the emitted surface and the manifest disagree:\n  "
                          + "\n  ".join(failures))
    assert any(p["default"] is not None
               for pr in declared_classes.values()
               for p in pr.get("ctor") or []), (
        "no constructor declares a default; that half proves nothing")
    assert ctor_checked >= len(wrapped) // 2, (
        f"checked only {ctor_checked} constructor(s) in the stubs")
    assert checked >= 3 * len(wrapped), (
        f"checked only {checked} method(s) across {len(wrapped)} classes")
    # Non-vacuity: something must actually HAVE a default, or the
    # comparison is between two empty lists everywhere.
    assert any(p["default"] is not None
               for group in ("wrappers", "returned_types")
               for pr in manifest[group].values()
               for m in pr["methods"] for p in m["params"]), (
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
      - the protocol offers those minus the ones the manifest blocked,
        and blocks nothing else;
      - parameter names and annotations are identical in all three
        (which is what tasks/025 bought: after it, a method on the
        protocol mentions no type that differs by location);
      - a return is identical in all three, unless the protocol names
        another protocol - then each implementation must return ITS
        form of that same class."""
    from huggorm_gen.pygen.generate import build_manifest

    manifest = build_manifest()
    wrapped = {
        name: proto
        for group in ("wrappers", "returned_types")
        for name, proto in manifest[group].items()
        if proto["wrapped"]
    }
    # protocol name -> the class it speaks for, so a protocol-typed
    # return can be checked against each implementation's own form.
    speaks_for = {proto["protocol"]: name for name, proto in wrapped.items()}
    twins: dict[str, str] = manifest.get("async_twins") or {}
    found = _emitted_classes(out)

    def blocked_over_chain(name: str | None, key: str) -> set[str]:
        """The methods `key` blocks, leaf definitions winning.

        Two keys, and they nest. wire_blockers means the RPC client
        cannot offer the method at all; protocol_blockers means the
        protocol cannot declare it, and every wire blocker is one of
        those - a protocol is what both implementations satisfy."""
        out_: dict[str, list[str]] = {}
        while name is not None:
            proto = wrapped[name]
            for m in proto["methods"]:
                out_.setdefault(m["name"], m[key])
            name = proto.get("async_base")
        return {n for n, why in out_.items() if why}

    failures, checked = [], 0
    for cls_name, proto in wrapped.items():
        P = _resolved(found, proto["protocol"])
        A = _resolved(found, proto["async_class"])
        R = _resolved(found, proto["rpc_class"])
        blocked = blocked_over_chain(cls_name, "protocol_blockers")
        no_wire = blocked_over_chain(cls_name, "wire_blockers")

        if set(R) != set(A) - no_wire:
            # The in-process surface is the larger one: a method the
            # wire cannot carry keeps its wrapper and is absent from
            # the client. Anything else is drift.
            failures.append(
                f"{cls_name}: in-process offers {sorted(set(A) - set(R))} "
                f"the rpc client does not, and {sorted(set(R) - set(A))} "
                f"the other way; the manifest blocks {sorted(no_wire)} "
                f"from the wire")
        if not no_wire <= blocked:
            failures.append(
                f"{cls_name}: {sorted(no_wire - blocked)} cannot cross the "
                f"wire and is still on the protocol")
        if set(P) != set(A) - blocked:
            failures.append(
                f"{cls_name}: {proto['protocol']} offers {sorted(P)}; the "
                f"implementations offer {sorted(A)} and the manifest blocks "
                f"{sorted(blocked)}")

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
                        f"{proto['protocol']} declares {want['params']}")
                if sig["annotations"] != want["annotations"]:
                    failures.append(
                        f"{cls_name}.{m}: {label} annotates "
                        f"{sig['annotations']}, {proto['protocol']} declares "
                        f"{want['annotations']}")
                if sig["defaults"] != want["defaults"]:
                    # A default is part of what a call MEANS. Three
                    # surfaces that agree on types and disagree here
                    # answer the same short call differently depending
                    # on where the object lives.
                    failures.append(
                        f"{cls_name}.{m}: {label} defaults to "
                        f"{sig['defaults']}, {proto['protocol']} declares "
                        f"{want['defaults']}")
                if not sig["is_async"]:
                    failures.append(f"{cls_name}.{m}: {label} is not async")
                expected = want["returns"]
                if expected in speaks_for:
                    other = wrapped[speaks_for[expected]]
                    expected = other["async_class"] if label == "in-process" \
                        else other["rpc_class"]
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
    assert checked >= 3 * len(wrapped), (
        f"conformance checked only {checked} method(s) across "
        f"{len(wrapped)} classes")
    for key, what in (("protocol_blockers", "protocol"),
                      ("wire_blockers", "wire")):
        assert any(blocked_over_chain(n, key) for n in wrapped), (
            f"no method is blocked from the {what}; either the rule "
            f"stopped working or the surface changed and this gate now "
            f"proves nothing")


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
                           and not f.name.startswith("_")}

        for name in classes:
            cls = getattr(module, name)
            stub_methods = surface(name)
            live_methods = set()
            for k in cls.__mro__:
                if getattr(k, "__module__", "").split(".")[0] != bindings.__name__:
                    continue
                live_methods |= {a for a, v in k.__dict__.items()
                                 if not a.startswith("_")
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
    - a caster takes a `Sequence` and answers a `list`. The
      declaration says `list` for both, which is the surface a caller
      sees.
    - a VOCABULARY is a StrEnum whose members ARE the strings
      libstore parses, so it crosses as `str` and nanobind says so.
      That is the whole point of declaring it as words rather than
      binding it, and `_vocabularies` is read from the manifest
      rather than listed here.
    - a UNION is an alias, and nanobind renders the arms it actually
      binds. `SingleDerivedPath` is `StorePath |
      SingleDerivedPathBuilt` by declaration, so the alias expands to
      exactly that and the two sides meet. Expanded REPEATEDLY,
      because an arm may itself name one - `_UNIONS` is read from the
      manifest, so nothing here lists an alias by hand.
    - an EXCEPTION a value holds crosses as `nb::object`, so nanobind
      says `object` and can say nothing else: a Python exception is
      not a bound C++ type and has no signature to render. The `|
      None` goes with it, because `nb::none()` IS the absent value
      there and the emitter writes no `std::optional` around it.
      `_ERRORS` is read from the manifest, like the two above."""
    out = re.sub(r"huggorm_bindings\.\w+\.", "", spelling)
    out = out.replace("collections.abc.Sequence[", "list[")
    for name in _ERRORS:
        out = re.sub(rf"\b{re.escape(name)}\b(\s*\|\s*None)?", "object", out)
    for name in _VOCABULARIES:
        out = re.sub(rf"\b{re.escape(name)}\b", "str", out)
    for _ in range(len(_UNIONS) + 1):
        before = out
        for alias, arms in _UNIONS.items():
            out = re.sub(rf"\b{re.escape(alias)}\b", " | ".join(arms), out)
        if out == before:
            break
    return re.sub(r"\s+", " ", out).strip()


_VOCABULARIES: set[str] = set()
# {alias: [arm, ...]}, from the manifest. See `_same`.
_UNIONS: dict[str, list[str]] = {}
# Declared EXCEPTION classes, from the manifest. See `_same`.
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
    from huggorm_gen.pygen.generate import build_manifest

    manifest = build_manifest()
    _VOCABULARIES.clear()
    _VOCABULARIES.update(manifest.get("enums", {}))
    _UNIONS.update(manifest.get("unions", {}))
    _ERRORS.clear()
    _ERRORS.update((manifest.get("errors") or {}).get("classes", {}))

    bad, checked = [], 0
    for group in ("wrappers", "returned_types"):
        for name, entry in manifest[group].items():
            cls = getattr(importlib.import_module(entry["module"]), name)
            for meth in entry["methods"]:
                fn = getattr(cls, meth["name"], None)
                sigs = getattr(fn, "__nb_signature__", None)
                if not sigs:
                    continue
                params = [_same(str(p["type"]))
                          # A parameter that reads None arrives as a
                          # std::optional so an explicit None works,
                          # and nanobind says so. The declaration
                          # says it with the default.
                          + (" | None" if p.get("default") == "None"
                             and not str(p["type"]).endswith("None")
                             else "")
                          for p in meth["params"]]
                want = (f"({', '.join(params)}) -> "
                        f"{_same(str(meth['return_type']))}")
                got = _rendered(sigs[0][0])
                checked += 1
                if got != want:
                    bad.append(f"{name}.{meth['name']}\n"
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
    from huggorm_gen.pygen.generate import build_manifest

    manifest = build_manifest()
    bad, seen = [], 0
    for group in ("wrappers", "returned_types"):
        for name, entry in manifest[group].items():
            cls = getattr(importlib.import_module(entry["module"]), name)
            methods = list(entry["methods"])
            if entry.get("ctor"):
                methods += entry["ctor"]
            for meth in methods:
                fn = getattr(cls, meth["name"], None)
                for sig, *_ in getattr(fn, "__nb_signature__", None) or ():
                    seen += 1
                    if "::" in sig:
                        bad.append(f"{name}.{meth['name']}: {sig}")
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
            asyncio.run(call(fn))
    print(f"smoke test OK ({len(checks)} checks)")


if __name__ == "__main__":
    main()
