# Async demo over real Nix. Mirrors the generated surface: pool stores
# overlap, affine states serialize, returned values inherit the
# producer's threading policy.

import tempfile
from typing import Any

import anyio

from huggorm_bindings import ContentAddressMethod as CA
from huggorm_bindings import HashAlgorithm, StorePath
from huggorm_generated import (
    AsyncEvalState,
    AsyncStore,
    collect_garbage,
    gc_stats,
)
from huggorm_generated._runtime import BaseRunner, InternalError


def _runner(obj: Any) -> BaseRunner:
    """The runner behind an object in this process."""
    backend = obj._backend
    assert isinstance(backend, BaseRunner)
    return backend


async def _add(store: AsyncStore, name: str, body: bytes) -> StorePath:
    return await store.add_to_store(name, body, CA.NAR, HashAlgorithm.SHA256)


async def main() -> None:
    root = tempfile.mkdtemp(prefix="huggorm-demo-")
    local = AsyncStore(root)

    print("=== sequential awaits ===")
    p = await _add(local, "hello.txt", b"world")
    # StorePath is pool and non-blocking, so the codegen wraps nothing:
    # the awaited store call hands back the binding object itself and
    # reading it is a plain call.
    print(p.to_string(), "valid:", await local.is_valid_path(p))

    print("\n=== GIL released during slow store ops ===")
    added: dict[str, StorePath] = {}

    async def add_one(name: str, body: bytes) -> None:
        added[name] = await _add(local, name, body)

    t0 = anyio.current_time()
    async with anyio.create_task_group() as tg:
        tg.start_soon(add_one, "a.txt", b"aaa")
        tg.start_soon(add_one, "b.txt", b"bbb")
    elapsed = anyio.current_time() - t0
    print(f"2x add_to_store at once: {elapsed * 1000:.0f}ms (parallel if << 200)")

    print("\n=== thread pool (Store, pool) ===")
    async with anyio.create_task_group() as tg:
        tg.start_soon(add_one, "c.txt", b"ccc")
        uri = await local.get_uri()
        valid = await local.is_valid_path(added["b.txt"])
    print(f"results: {[uri, added['c.txt'].to_string(), valid]}")
    print(f"workers seen: {sorted(_runner(local).workers_seen)}")

    print("\n=== wire values off a real store ===")
    info = await local.query_path_info(added["a.txt"])
    print(f"nar_size: {info.nar_size()}  hash: {info.nar_hash().to_string()[:24]}...")

    print("\n=== evaluation (EvalState, affine service) ===")
    state = AsyncEvalState(AsyncStore("dummy://"))
    print(f"store uri: {await state.get_store_uri()}")
    print(f"born on:   {_runner(state).born_thread_name}")

    thunk = await state.parse_expr("42")
    print(f"parsed: type={await thunk.type_name()}")
    try:
        await thunk.integer()
        print("should not happen")
    except InternalError as e:
        print(f"unforced access fails: {e.__cause__}")
    await state.force(thunk)
    print(f"forced: type={await thunk.type_name()}, value={await thunk.integer()}")

    v = await state.eval_expr('"hello nix"')
    print(f"eval: {await v.string_value()!r} (type {await v.type_name()})")

    print("\n=== returned values inherit threading ===")
    print(f"value workers: {sorted(_runner(v).workers_seen)} "
          f"(state's: {sorted(_runner(state).workers_seen)})")
    print(f"workers seen: {sorted(_runner(state).workers_seen)}  <- must be exactly 1")

    # Module-level binding functions get generated wrappers too.
    await collect_garbage()
    stats = await gc_stats()
    print(f"after 2x full GC: {await v.string_value()!r}")
    print(f"gc: {stats['collections']} collections, heap {stats['heap_size'] >> 10} KiB,"
          f" value in GC heap: {await v.is_gc_managed()}")

    async def evaluate(expr: str) -> None:
        await state.eval_expr(expr)

    t0 = anyio.current_time()
    async with anyio.create_task_group() as tg:
        tg.start_soon(evaluate, "1")
        tg.start_soon(evaluate, "2")
    elapsed = anyio.current_time() - t0
    print(f"2x eval_expr at once: {elapsed * 1000:.0f}ms (one dedicated thread)")

    print("\n=== a Nix error surfaces as InternalError with its cause ===")
    try:
        await state.eval_expr("not an expression")
        print("should not happen")
    except InternalError as e:
        print(f"caught InternalError: {e.to_dict()}")

    await v.aclose()
    await thunk.aclose()
    await state.aclose()

    print("\n=== one function, either location, no branching ===")

    async def report(store: AsyncStore) -> str:
        # Typed against the protocol. Everything it calls is on the
        # generated surface, so it never asks whether the store is in
        # this process or on the far side of a socket.
        path = await _add(store, "shared.txt", b"either location")
        return f"{await store.get_uri()}: {path.to_string()}"

    print(await report(local))

    print("\n=== constructors are typed, so arity fails at the call site ===")
    try:
        # Deliberately wrong, and a typechecker says so - which is the
        # point being demonstrated. The ignore is what makes the demo
        # runnable AND checkable.
        AsyncEvalState(AsyncStore("dummy://"), None, None, "extra")  # type: ignore[call-arg]
        print("should not happen")
    except TypeError as e:
        print(f"AsyncEvalState(store, None, None, 'extra') -> TypeError: {e}")
    try:
        AsyncEvalState()  # type: ignore[call-arg]
        print("should not happen")
    except TypeError as e:
        print(f"AsyncEvalState() -> TypeError: {e}")

    await local.aclose()
    print("closed cleanly")


if __name__ == "__main__":
    anyio.run(main)
