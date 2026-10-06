"""The async runtime under the emitted wrappers: runners, threads and
the collector.

Hermetic: a chroot store and `dummy://` need no daemon.
"""

import gc
import importlib
import pathlib
import types
from typing import Any

import anyio
import pytest
from nixversion import HAS_COLLECTOR, needs_collector

URI = "dummy://"


async def _all(*aws: Any) -> list[Any]:
    """Every awaitable concurrently, results in order.

    anyio has no `gather`: a task group OWNS its children, so a failure
    in one cancels the rest rather than coming back as a value."""
    out: list[Any] = [None] * len(aws)

    async def one(i: int, aw: Any) -> None:
        out[i] = await aw

    async with anyio.create_task_group() as tg:
        for i, aw in enumerate(aws):
            tg.start_soon(one, i, aw)
    return out


async def _all_errors(*aws: Any) -> list[Any]:
    """As `_all`, but every awaitable is EXPECTED to fail.

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


def _shell(runner: Any) -> Any:
    """The two attributes `unwrap_arg` reads off a wrapper."""
    return types.SimpleNamespace(_runner=runner, _copied=False)


async def collect() -> None:
    import huggorm_generated

    await huggorm_generated.collect_garbage()


async def test_a_pool_store_hands_back_a_store_path_itself(
        tmp_path: pathlib.Path) -> None:
    """StorePath is pool AND non-blocking, so it has no wrapper: an
    awaited store method hands back the binding object itself, and
    reading it is a plain call (huggorm#25)."""
    from huggorm_bindings import ContentAddressMethod as CA
    from huggorm_bindings import HashAlgorithm, StorePath
    from huggorm_generated import AsyncStore

    local = AsyncStore(str(tmp_path))
    assert await local.query_all_valid_paths() == [], "a fresh chroot is empty"
    p1, p2 = await _all(
        local.add_to_store("hello.txt", b"world", CA.NAR, HashAlgorithm.SHA256),
        local.add_to_store("note.txt", b"nix real", CA.NAR, HashAlgorithm.SHA256),
    )
    assert type(p1) is StorePath
    assert p1.to_string() != p2.to_string()
    assert await local.is_valid_path(p1) is True
    assert local._copied is False and p1._copied is True
    await local.aclose()


async def test_a_pool_runner_constructs_once_under_concurrent_first_calls(
        ) -> None:
    """Each stray factory call once built a diverging underlying
    store, through an unlocked `_resolve`."""
    from huggorm_generated import _runtime

    class Probe:
        def noop(self) -> str:
            return "ok"

    made: list[int] = []

    def factory() -> Probe:
        made.append(1)
        return Probe()

    runner = _runtime.PoolRunner(factory)
    assert await _all(*(runner.call("noop", []) for _ in range(8))) == ["ok"] * 8
    assert len(made) == 1, f"factory ran {len(made)}x under concurrent first-calls"


async def test_a_failing_factory_runs_once_and_every_caller_sees_why() -> None:
    from huggorm_generated import _runtime
    from huggorm_generated._runtime import InternalError

    failed: list[int] = []

    def bad_factory() -> object:
        failed.append(1)
        raise RuntimeError("no")

    runner = _runtime.PoolRunner(bad_factory)
    errs = await _all_errors(*(runner.call("noop", []) for _ in range(4)))
    assert len(failed) == 1, f"failing factory ran {len(failed)}x"
    assert all(isinstance(e, InternalError) for e in errs)
    assert all(type(e.__cause__) is RuntimeError for e in errs)


async def test_an_unbuilt_affine_object_is_built_on_its_own_thread() -> None:
    """An affine wrapper that has never been called refuses to be built
    on a foreign thread. Its first call builds it on its own thread,
    and after that it unwraps from anywhere. A pool object builds
    lazily from any thread."""
    from huggorm_generated import _runtime
    from huggorm_generated._runtime import InternalError

    lazy = _shell(_runtime.AffineRunner(lambda: object()))
    with pytest.raises(TypeError):
        _runtime.unwrap_arg(lazy)

    with pytest.raises(InternalError):
        await lazy._runner.call("noop", [])
    assert _runtime.unwrap_arg(lazy) is not None
    assert lazy._runner.born_thread_name.startswith("huggorm-affine")

    pool = _shell(_runtime.PoolRunner(lambda: {"ok": True}))
    assert _runtime.unwrap_arg(pool) == {"ok": True}


async def test_an_untouched_evaluator_is_born_on_its_own_thread() -> None:
    from huggorm_generated import AsyncEvalState, AsyncStore

    untouched = AsyncEvalState(AsyncStore(URI))
    assert untouched._runner._obj is None, "expected an unconstructed wrapper"
    assert await (await untouched.eval_expr("1")).integer() == 1
    born = untouched._runner.born_thread_name
    assert born is not None and born.startswith("huggorm-affine"), born
    await untouched.aclose()


async def test_an_evaluator_and_its_values_share_one_thread() -> None:
    """Proved by WHERE the calls ran, not by how long they took: libexpr
    evaluates "1" in microseconds, so a stopwatch measures the
    scheduler."""
    from huggorm_generated import AsyncEvalState, AsyncStore, AsyncValue

    state = AsyncEvalState(AsyncStore(URI))
    assert await state.get_store_uri() == URI
    await _all(state.eval_expr("1"), state.eval_expr("2"))
    assert len(state._runner.workers_seen) == 1, state._runner.workers_seen

    value = await state.make_int(11)
    assert isinstance(value, AsyncValue)
    assert await value.integer() == 11
    assert await value.type_name() == "int"
    assert value._runner.workers_seen == state._runner.workers_seen
    await value.aclose()
    await state.aclose()


async def test_a_thunk_refuses_access_until_forced() -> None:
    from huggorm_bindings.errors import NixTypeError
    from huggorm_generated import AsyncEvalState, AsyncStore

    state = AsyncEvalState(AsyncStore(URI))
    thunk = await state.parse_expr("42")
    assert await thunk.type_name() == "thunk"
    with pytest.raises(NixTypeError, match="expected an integer but found a thunk"):
        await thunk.integer()
    await state.force(thunk)
    assert await thunk.type_name() == "int"
    assert await thunk.integer() == 42
    await state.force(thunk)
    assert await thunk.integer() == 42, "force is idempotent"
    await state.aclose()


async def test_an_error_arrives_as_itself_or_as_internal_error() -> None:
    """A nix::Error is DECLARED, so `except NixError` works here exactly
    as against the sync binding (huggorm#66). Anything else carries no
    parts and arrives as an InternalError naming it."""
    from huggorm_generated import AsyncEvalState, AsyncStore
    from huggorm_generated._policy import ERROR_MODULE
    from huggorm_generated._runtime import InternalError

    # By its DERIVED name: the errors module is named after the
    # declaration (huggorm#63).
    NixError = importlib.import_module(ERROR_MODULE).NixError

    state = AsyncEvalState(AsyncStore(URI))
    with pytest.raises(NixError) as declared:
        await state.eval_expr("not an expression")
    d = declared.value.to_dict()
    assert d["code"] == "UndefinedVarError", d
    assert "undefined variable" in d["message"], d

    for refused in (state.parse_expr(""), state.eval_expr("")):
        with pytest.raises(InternalError) as internal:
            await refused
        d = internal.value.to_dict()
        assert d["code"] == "internal" and d["cause_type"] == "ValueError", d
    await state.aclose()


async def test_an_attribute_set_is_built_in_name_order() -> None:
    """Nix attribute sets are alphabetical, so an index walk IS the
    listing order. The C++ side keeps a sorted array, like
    nix::Bindings."""
    from huggorm_generated import AsyncEvalState, AsyncStore

    builder = AsyncEvalState(AsyncStore(URI))
    attrs = await builder.make_attrs()
    for name, number in (("zebra", 1), ("apple", 2), ("mango", 3)):
        await builder.attrs_set(attrs, name, await builder.make_int(number))
    assert await attrs.type_name() == "attrs"
    assert await attrs.size() == 3
    assert [await attrs.name_at(i) for i in range(3)] == ["apple", "mango", "zebra"]
    assert [await (await attrs.value_at(i)).integer() for i in range(3)] == [2, 3, 1]
    assert await attrs.has("mango") and not await attrs.has("durian")

    # Setting a name twice replaces its value, like assignment.
    await builder.attrs_set(attrs, "apple", await builder.make_int(99))
    assert await attrs.size() == 3
    assert await (await attrs.get("apple")).integer() == 99

    xs = await builder.make_list()
    for word in ("one", "two"):
        await builder.list_append(xs, await builder.make_string(word))
    await builder.attrs_set(attrs, "xs", xs)
    assert await (await (await attrs.get("xs")).at(1)).string_value() == "two"
    await builder.aclose()


@pytest.mark.parametrize("access", ["integer", "at", "name_at"])
async def test_a_wrong_access_says_which(access: str) -> None:
    from huggorm_generated import AsyncEvalState, AsyncStore

    builder = AsyncEvalState(AsyncStore(URI))
    attrs = await builder.make_attrs()
    await builder.attrs_set(attrs, "a", await builder.make_int(1))
    call = {"integer": lambda: attrs.integer(), "at": lambda: attrs.at(0),
            "name_at": lambda: attrs.name_at(99)}[access]
    with pytest.raises(Exception) as refused:
        await call()
    # The runtime wraps a non-Nix binding failure in InternalError, so
    # its C++ text is on the cause, not the message.
    why = str(refused.value.__cause__ or refused.value)
    assert "but found" in why or "out of range" in why, why
    await builder.aclose()


@needs_collector
async def test_children_survive_collection_through_the_parent() -> None:
    """The collector must SEE the children through the parent. A plain
    std::vector<Value *> inside a GC-allocated Value holds them in
    malloc memory, which Boehm does not scan: they would be collected
    while the parent still pointed at them. So drop every Python
    reference, collect, churn until a freed block would be reused, and
    read the tree back."""
    from huggorm_generated import AsyncEvalState, AsyncStore

    builder = AsyncEvalState(AsyncStore(URI))
    attrs = await builder.make_attrs()
    await builder.attrs_set(attrs, "apple", await builder.make_int(99))
    xs = await builder.make_list()
    for word in ("one", "two"):
        await builder.list_append(xs, await builder.make_string(word))
    await builder.attrs_set(attrs, "xs", xs)

    del xs
    gc.collect()
    await collect()
    churn = [await builder.make_int(i) for i in range(500)]
    del churn
    gc.collect()
    await collect()
    assert await attrs.size() == 2
    assert await (await attrs.get("apple")).integer() == 99
    assert await (await (await attrs.get("xs")).at(0)).string_value() == "one"
    await builder.aclose()


@needs_collector
async def test_closing_an_affine_wrapper_leaves_its_thread_unregistered(
        ) -> None:
    """A closed affine wrapper's thread leaves the collector's list
    before it dies. Boehm stops the world by signalling every
    registered thread and waiting for each; a dead one never answers,
    and the next collection aborted the PROCESS with "Signals delivery
    fails constantly"."""
    import huggorm_bindings
    from huggorm_generated import AsyncEvalState, AsyncStore

    for _ in range(2):
        state = AsyncEvalState(AsyncStore(URI))
        await state.eval_expr("1")
        await state.aclose()
    await collect()
    assert huggorm_bindings.gc_stats()["heap_size"] > 0


@needs_collector
async def test_a_value_lives_in_the_collector_and_survives_it() -> None:
    """The counters bound from gc.h prove the collector is ACTIVE and
    that this value lives in a GC-allocated block; survival proves it
    is rooted. A no-op integration could produce neither."""
    import huggorm_bindings
    from huggorm_generated import AsyncEvalState, AsyncStore

    state = AsyncEvalState(AsyncStore(URI))
    v = await state.eval_expr('"hello nix"')
    thunk = await state.parse_expr("42")
    await state.force(thunk)
    stats = huggorm_bindings.gc_stats()
    assert stats["heap_size"] > 0 and stats["total_bytes"] > 0
    assert await v.is_gc_managed()
    assert await thunk.is_gc_managed()

    await collect()
    await collect()
    assert huggorm_bindings.gc_stats()["collections"] >= stats["collections"] + 2
    assert await v.string_value() == "hello nix"
    assert await thunk.integer() == 42
    fresh = await state.parse_expr("7")
    await state.force(fresh)
    assert await fresh.integer() == 7
    await state.aclose()


@pytest.mark.skipif(HAS_COLLECTOR, reason="this Nix has the collector")
async def test_nothing_is_collector_managed_without_one() -> None:
    from huggorm_generated import AsyncEvalState, AsyncStore

    state = AsyncEvalState(AsyncStore(URI))
    assert not await (await state.eval_expr("1")).is_gc_managed()
    await state.aclose()


@needs_collector
async def test_a_dropped_value_releases_its_root() -> None:
    """OUR invariant, not a heap-bytes one. An evaluated value is rooted
    by the STATE too, and boehm is conservative, so a byte count is
    flaky by construction. A leaked root keeps its value alive forever,
    and only this count sees it."""
    import huggorm_bindings
    from huggorm_generated import AsyncEvalState, AsyncStore

    state = AsyncEvalState(AsyncStore(URI))
    await state.eval_expr("1")
    before = huggorm_bindings.gc_stats()["live_roots"]
    kept = [await state.make_string(f'{"p" * 200}-{i}') for i in range(200)]
    assert huggorm_bindings.gc_stats()["live_roots"] >= before + 200

    del kept
    await collect()
    assert huggorm_bindings.gc_stats()["live_roots"] == before
    await state.aclose()


async def test_a_wrapper_checks_its_arity_at_construction() -> None:
    """At the call site, not from inside the lazy factory on a worker
    thread at the first method call."""
    from huggorm_generated import AsyncEvalState, AsyncStore

    with pytest.raises(TypeError):
        AsyncEvalState(AsyncStore(URI), None, None, "unexpected-arg")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        AsyncEvalState()  # type: ignore[call-arg]
