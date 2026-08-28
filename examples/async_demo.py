# Async demo over real Nix, with the evaluator still on the mock.
# Mirrors the generated surface: pool stores overlap, affine states
# serialize, returned values inherit the producer's threading policy.

import asyncio
import tempfile

from cythonix_bindings import ContentAddressMethod as CA
from cythonix_bindings import HashAlgorithm
from cythonix_generated import (
    AsyncEvalState,
    AsyncStore,
    StoreLike,
)
from cythonix_generated._runtime import InternalError


def _add(store: AsyncStore, name: str, body: bytes) -> object:
    return store.add_to_store(name, body, CA.NAR, HashAlgorithm.SHA256)


async def main() -> None:
    root = tempfile.mkdtemp(prefix="cythonix-demo-")
    local = AsyncStore(root)

    print("=== sequential awaits ===")
    p = await _add(local, "hello.txt", b"world")
    # StorePath is pool and non-blocking, so the codegen wraps nothing:
    # the awaited store call hands back the binding object itself and
    # reading it is a plain call.
    print(p.to_string(), "valid:", await local.is_valid_path(p))

    print("\n=== GIL released during slow store ops ===")
    t0 = asyncio.get_running_loop().time()
    a, b = await asyncio.gather(
        _add(local, "a.txt", b"aaa"),
        _add(local, "b.txt", b"bbb"),
    )
    elapsed = asyncio.get_running_loop().time() - t0
    print(f"2x add_to_store gathered: {elapsed * 1000:.0f}ms (parallel if << 200)")

    print("\n=== thread pool (Store, pool) ===")
    results = await asyncio.gather(
        local.get_uri(),
        _add(local, "c.txt", b"ccc"),
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

    print("\n=== wire values off a real store ===")
    info = await local.query_path_info(a)
    print(f"nar_size: {info.nar_size()}  hash: {info.nar_hash().to_string()[:24]}...")

    print("\n=== evaluation (EvalState, affine service) ===")
    state = AsyncEvalState("local")
    print(f"store uri: {await state.get_store_uri()}")
    print(f"born on:   {state._runner.born_thread_name}")

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
    print(f"value workers: {sorted(v._runner.workers_seen)} "
          f"(state's: {sorted(state._runner.workers_seen)})")
    print(f"workers seen: {sorted(state._runner.workers_seen)}  <- must be exactly 1")

    # Module-level binding functions get generated wrappers too, so the
    # hand-written asyncio.to_thread hop is gone.
    from cythonix_generated import collect_garbage, gc_stats

    await collect_garbage()
    stats = await gc_stats()
    print(f"after 2x full GC: {await v.string_value()!r}")
    print(f"gc: {stats['collections']} collections, heap {stats['heap_size'] >> 10} KiB,"
          f" value in GC heap: {await v.is_gc_managed()}")

    t0 = asyncio.get_running_loop().time()
    await asyncio.gather(state.eval_expr("1"), state.eval_expr("2"))
    elapsed = asyncio.get_running_loop().time() - t0
    print(f"2x eval_expr gathered: {elapsed * 1000:.0f}ms (>=80: one dedicated thread)")

    print("\n=== C++ exception surfaces as InternalError with cause chain ===")
    try:
        await state.eval_expr("not an expression")
        print("should not happen")
    except InternalError as e:
        print(f"caught InternalError: {e.to_dict()}")

    await v.aclose()
    await thunk.aclose()
    await state.aclose()

    print("\n=== one function, either location, no branching ===")

    async def report(store: StoreLike) -> str:
        # Typed against the protocol. Everything it calls is on the
        # generated surface, so it never asks whether the store is in
        # this process or on the far side of a socket.
        path = await store.add_to_store("shared.txt", b"either location",
                                        CA.NAR, HashAlgorithm.SHA256)
        return f"{await store.get_uri()}: {path.to_string()}"

    print(await report(local))

    print("\n=== constructors are typed, so arity fails at the call site ===")
    # The wrapper states its constructor parameters, from the
    # declaration that also wrote the binding.
    # A wrong call used to sail through __init__(*args) and surface much
    # later, from inside the lazy factory on a worker thread.
    try:
        # Deliberately wrong, and a typechecker says so - which is the
        # point being demonstrated. The ignore is what makes the demo
        # runnable AND checkable.
        AsyncEvalState("local", "unexpected-arg")  # type: ignore[call-arg]
        print("should not happen")
    except TypeError as e:
        print(f"AsyncEvalState('local', 'unexpected-arg') -> TypeError: {e}")
    try:
        AsyncEvalState()  # type: ignore[call-arg]
        print("should not happen")
    except TypeError as e:
        print(f"AsyncEvalState() -> TypeError: {e}")

    await local.aclose()
    print("closed cleanly")


if __name__ == "__main__":
    asyncio.run(main())
