"""The async runtime under the emitted wrappers: runners, threads and
the collector.

Hermetic: a chroot store and `dummy://` need no daemon.
"""

import functools
import gc
import importlib
import pathlib
import time
import types
from typing import Any

import anyio
import pytest
from nixversion import HAS_COLLECTOR, UNDEFINED_VARIABLE, needs_collector

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


def _probe(method: str) -> Any:
    """A spec for a method of a test's own target, which no
    declaration names."""
    from huggorm_generated._callspec import Local

    return Local(method, None, "a test's own target")


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
    assert await _all(*(runner.call(_probe("noop"), []) for _ in range(8))) == ["ok"] * 8
    assert len(made) == 1, f"factory ran {len(made)}x under concurrent first-calls"


async def test_a_failing_factory_runs_once_and_every_caller_sees_why() -> None:
    from huggorm_generated import _runtime
    from huggorm_generated._runtime import InternalError

    failed: list[int] = []

    def bad_factory() -> object:
        failed.append(1)
        raise RuntimeError("no")

    runner = _runtime.PoolRunner(bad_factory)
    errs = await _all_errors(*(runner.call(_probe("noop"), []) for _ in range(4)))
    assert len(failed) == 1, f"failing factory ran {len(failed)}x"
    assert all(isinstance(e, InternalError) for e in errs)
    assert all(type(e.__cause__) is RuntimeError for e in errs)


def test_a_returned_none_is_not_adopted() -> None:
    """`X | None` adopts X and passes None through. Adopting None
    builds a wrapper around nothing, which fails only at the first
    await on it."""
    from huggorm_generated import AsyncStore, _runtime
    from huggorm_generated._callspec import Wire, WireKind

    runner = _runtime.PoolRunner(None)
    returns = Wire(WireKind.PROXY, "Store", optional=True)
    assert runner._adopted(returns, None) is None
    assert isinstance(runner._adopted(returns, object()), AsyncStore)


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
        await lazy._runner.call(_probe("noop"), [])
    assert _runtime.unwrap_arg(lazy) is not None
    assert lazy._runner.born_thread_name.startswith("huggorm-affine")

    pool = _shell(_runtime.PoolRunner(lambda: {"ok": True}))
    assert _runtime.unwrap_arg(pool) == {"ok": True}


async def test_calls_on_one_thread_run_in_the_order_they_were_made(
        ) -> None:
    """A server runs the calls of one state in the order a client sent
    them. Building an argument, or the target itself, must not let a
    later call go first (huggorm#155)."""
    from huggorm_generated import _runtime

    order: list[str] = []

    class Target:
        def note(self, label: str, *_: Any) -> None:
            order.append(label)

    def slowly_built() -> object:
        time.sleep(0.2)
        return object()

    runner = _runtime.AffineRunner(Target)
    unbuilt = _shell(_runtime.PoolRunner(slowly_built))
    async with anyio.create_task_group() as tg:
        tg.start_soon(runner.call, _probe("note"), ["argument", unbuilt])
        await anyio.sleep(0)
        tg.start_soon(runner.call, _probe("note"), ["after the argument"])
    fresh = _runtime.AffineRunner(Target)
    async with anyio.create_task_group() as tg:
        tg.start_soon(fresh.run, lambda target: target.note("run"))
        await anyio.sleep(0)
        tg.start_soon(fresh.call, _probe("note"), ["after the run"])
    assert order == ["argument", "after the argument", "run", "after the run"]
    await runner.aclose()
    await fresh.aclose()


def test_a_waiting_thread_runs_what_arrives_while_it_waits() -> None:
    """Work submitted while the thread waits runs on it, inside the
    wait. Work queued before the wait runs after the job that waits
    (huggorm#155)."""
    import concurrent.futures
    import threading

    from huggorm_generated import _runtime

    home = _runtime._Home("huggorm-test-home")
    done: concurrent.futures.Future[None] = concurrent.futures.Future()
    started, go = threading.Event(), threading.Event()
    order: list[str] = []

    def waits() -> int:
        started.set()
        go.wait()
        order.append("waits")
        home.wait(done)
        order.append("waited")
        return threading.get_ident()

    waiting = home.submit(waits)
    assert started.wait(5)
    before = home.submit(order.append, "queued before")
    go.set()
    try:
        deadline = time.monotonic() + 5
        while home._inbox is None:
            assert time.monotonic() < deadline, "the thread never waited"
            time.sleep(0.01)
        def inside_the_wait() -> int:
            order.append("inside")
            return threading.get_ident()

        inside = home.submit(inside_the_wait)
        ident = inside.result(5)
    finally:
        # A thread still waiting would hold the interpreter at exit.
        done.set_result(None)
    assert waiting.result(5) == ident
    before.result(5)
    assert order == ["waits", "inside", "waited", "queued before"]
    home.shutdown()


def test_a_pool_thread_that_waits_does_not_count() -> None:
    """A one-thread pool whose thread waits for work it submits: the
    pool starts a thread for that work, and retires it after
    (huggorm#155)."""
    import concurrent.futures

    from huggorm_generated import _runtime

    pool = _runtime._Pool(1, name="huggorm-test-pool")
    done: concurrent.futures.Future[str] = concurrent.futures.Future()

    def answer() -> None:
        pool.submit(done.set_result, "answered")

    def waits() -> str:
        pool.wait(done, answer)
        return done.result()

    try:
        assert pool.submit(waits).result(5) == "answered"
        # The next job runs on the one thread the size allows.
        assert pool.submit(lambda: "again").result(5) == "again"
        # The surplus thread retires after its job, not with it.
        deadline = time.monotonic() + 5
        while len(pool._threads) != 1:
            assert time.monotonic() < deadline, pool._threads
            time.sleep(0.01)
    finally:
        if not done.done():
            done.set_result("released")
        pool.shutdown()


async def test_a_worker_goes_on_while_a_posted_hook_waits() -> None:
    """The worker queues posted hooks up to the window while the loop
    runs the first, and its call ends only once all have run
    (huggorm#155)."""
    from huggorm_generated import _runtime

    window = _runtime._POSTED_WINDOW
    gate = anyio.Event()
    posted: list[int] = []
    ran: list[int] = []

    async def write(i: int) -> None:
        await gate.wait()
        ran.append(i)

    def work() -> None:
        hooks = _runtime._CALLING.hooks
        for i in range(3 * window):
            hooks.post(functools.partial(write, i))
            posted.append(i)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_runtime.call_function, work, [])
        with anyio.fail_after(10):
            while len(posted) < window:
                await anyio.sleep(0.01)
        await anyio.sleep(0.1)
        assert len(posted) == window
        assert ran == []
        gate.set()
    assert ran == list(range(3 * window))


async def test_a_failed_posted_hook_stops_the_ones_after_it() -> None:
    from huggorm_generated import _runtime

    ran: list[int] = []

    async def write(i: int) -> None:
        if i == 3:
            raise RuntimeError("the disk is full")
        ran.append(i)

    def work() -> None:
        hooks = _runtime._CALLING.hooks
        for i in range(8):
            hooks.post(functools.partial(write, i))

    with anyio.fail_after(10), pytest.raises(Exception) as caught:
        await _runtime.call_function(work, [])
    assert "the disk is full" in str(caught.value.__cause__)
    assert ran == [0, 1, 2]


async def test_a_posted_hook_may_call_the_state_that_posts_it() -> None:
    """On a home thread a posted hook is asked: posted, it would run
    while the thread works, and its call would queue behind the call
    that waits for it."""
    from huggorm_generated import AsyncEvalState, AsyncStore, _runtime

    state = AsyncEvalState(AsyncStore(URI))
    answers: list[int] = []

    async def write(i: int) -> None:
        answers.append(await (await state.eval_expr(f"{i} + 1")).integer())

    def work(_: Any) -> None:
        hooks = _runtime._CALLING.hooks
        for i in range(3):
            hooks.post(functools.partial(write, i))

    with anyio.fail_after(10):
        await state._runner.run(work)
    assert answers == [1, 2, 3]
    await state.aclose()


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
    assert UNDEFINED_VARIABLE in d["message"], d

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


async def test_closing_a_busy_affine_wrapper_leaves_the_loop_free() -> None:
    """`aclose` waits for the thread's queue to drain, and that wait
    must not hold the event loop: a server closes a dropped state while
    its thread still evaluates, and every other client waits on the
    same loop (huggorm#143). A timer, not the loop, ends the work."""
    import threading
    import time

    from huggorm_generated import _runtime

    release = threading.Event()

    class Busy:
        def work(self) -> None:
            release.wait()

    runner = _runtime.AffineRunner(Busy)
    timer = threading.Timer(2.0, release.set)
    timer.start()
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(runner.call, _probe("work"), [])
            await anyio.sleep(0.1)
            tg.start_soon(runner.aclose)
            started = time.monotonic()
            await anyio.sleep(0.05)
            waited = time.monotonic() - started
    finally:
        release.set()
        timer.cancel()
    assert waited < 1.0, f"the loop stalled {waited:.2f}s behind aclose"


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
    # Garbage an earlier test left would leave the count during the test.
    await collect()
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
