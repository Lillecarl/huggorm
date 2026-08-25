# Async demo over the Nix-store mock. Mirrors the generated surface:
# pool stores overlap, affine stores serialize, returned values inherit
# the producer's threading policy.

import asyncio

from cythonix_bindings import DerivedPath
from cythonix_generated import (
    AsyncEvalState,
    AsyncLocalStore,
    AsyncRemoteStore,
    AsyncStore,
)
from cythonix_generated._runtime import InternalError


async def main() -> None:
    local = AsyncLocalStore()
    remote = AsyncRemoteStore()

    print("=== sequential awaits ===")
    p = await local.add_text_to_store("hello.txt", "world")
    # StorePath is pool and non-blocking, so the codegen wraps nothing:
    # the awaited store call hands back the binding object itself and
    # reading it is a plain call.
    print(p.to_string(), "valid:", await local.is_valid_path(p))

    print("\n=== GIL released during slow store ops ===")
    t0 = asyncio.get_running_loop().time()
    a, b = await asyncio.gather(
        local.add_text_to_store("a.txt", "aaa"),
        local.add_text_to_store("b.txt", "bbb"),
    )
    elapsed = asyncio.get_running_loop().time() - t0
    print(f"2x add_text_to_store gathered: {elapsed * 1000:.0f}ms (parallel if << 200)")

    print("\n=== thread affinity (RemoteStore, affine) ===")
    await remote.get_uri()
    await remote.is_valid_path(a)
    await remote.add_text_to_store("slow.drv", "DrvSlow")
    print(f"born on:   {remote._runner.born_thread_name}")
    print(f"last call: {remote._runner.last_worker_name}")
    print(f"workers seen: {sorted(remote._runner.workers_seen)}  <- must be exactly 1")

    print("\n=== thread pool (LocalStore, pool) ===")
    results = await asyncio.gather(
        local.get_uri(),
        local.add_text_to_store("c.txt", "ccc"),
        local.is_valid_path(b),
    )
    printed = []
    for r in results:
        if isinstance(r, str | bool):
            printed.append(r)
        else:
            printed.append(r.to_string())
    print(f"results: {printed}")
    print(f"workers seen: {sorted(local._runner.workers_seen)}")

    print("\n=== returned values inherit threading ===")
    drv_path = await remote.add_text_to_store("mysite.drv", "DrvMine")
    drv = await remote.query_derivation(drv_path)
    print(await drv.describe())
    print(await drv.describe())
    print(f"drv workers: {sorted(drv._runner.workers_seen)} "
          f"(store's: {sorted(remote._runner.workers_seen)})")
    print(f"queries: {await drv.queries()}")

    out = await local.build_derivation(DerivedPath(drv_path, "out"))
    print(f"built: {out.to_string()} valid: {await local.is_valid_path(out)}")

    print("\n=== evaluation (EvalState, affine service) ===")
    state = AsyncEvalState("local")
    print(f"store uri: {await state.get_store_uri()}")

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

    # Module-level binding functions get generated wrappers too, so the
    # hand-written asyncio.to_thread hop is gone.
    from cythonix_generated import collect_garbage, describe, gc_stats

    await collect_garbage()
    stats = await gc_stats()
    print(f"after 2x full GC: {await v.string_value()!r}")
    print(f"gc: {stats['collections']} collections, heap {stats['heap_size'] >> 10} KiB,"
          f" value in GC heap: {await v.is_gc_managed()}")

    print(f"value workers: {sorted(v._runner.workers_seen)} "
          f"(state's: {sorted(state._runner.workers_seen)})")

    t0 = asyncio.get_running_loop().time()
    await asyncio.gather(state.eval_expr("1"), state.eval_expr("2"))
    elapsed = asyncio.get_running_loop().time() - t0
    print(f"2x eval_expr gathered: {elapsed * 1000:.0f}ms (>=80: one dedicated thread)")

    try:
        await state.eval_expr("not an expression")
        print("should not happen")
    except InternalError as e:
        print(f"caught InternalError: {e.to_dict()}")

    await v.aclose()
    await thunk.aclose()
    await state.aclose()

    print("\n=== one function, either store, no branching ===")

    async def report(store: AsyncStore) -> str:
        # Typed against the base. Everything it calls is guaranteed by
        # every implementation, so it never asks which one it holds.
        path = await store.add_text_to_store("shared.txt", "either store")
        return f"{await store.get_uri()}: {path.to_string()}"

    print(await report(local))
    print(await report(remote))
    print("AsyncLocalStore is an AsyncStore:", isinstance(local, AsyncStore))
    print("query_derivation is not guaranteed, so it is on RemoteStore only:",
          hasattr(remote, "query_derivation"), "/", hasattr(local, "query_derivation"))

    print("\n=== free functions (C++ virtual dispatch through a wrapper) ===")
    print("describe(local): ", await describe(local))
    print("describe(remote):", await describe(remote))

    print("\n=== policy enforcement ===")
    print("AsyncLocalStore exposes query_derivation:", hasattr(local, "query_derivation"),
          "<- False: pool wrapper may not return the affine Derivation")

    print("\n=== C++ exception surfaces as InternalError with cause chain ===")
    try:
        await remote.query_derivation(p)
        print("should not happen")
    except InternalError as e:
        print("caught InternalError:", e.to_dict())

    print("\n=== constructors are typed, so arity fails at the call site ===")
    # The wrapper states its constructor parameters, taken from the pxd.
    # A wrong call used to sail through __init__(*args) and surface much
    # later, from inside the lazy factory on a worker thread.
    try:
        # Deliberately wrong, and a typechecker says so - which is the
        # point being demonstrated. The ignore is what makes the demo
        # runnable AND checkable.
        AsyncRemoteStore("unexpected-arg")  # type: ignore[call-arg]
        print("should not happen")
    except TypeError as e:
        print(f"AsyncRemoteStore('unexpected-arg') -> TypeError: {e}")
    try:
        AsyncEvalState()  # type: ignore[call-arg]
        print("should not happen")
    except TypeError as e:
        print(f"AsyncEvalState() -> TypeError: {e}")

    await drv.aclose()
    await remote.aclose()
    await local.aclose()
    print("closed cleanly")


if __name__ == "__main__":
    asyncio.run(main())
