"""
Verify the freshly generated package. Installed as the `codegen-smoke`
entry point; runs after codegen-generate, stdlib only:

1. every emitted .py parses
2. the package imports and __all__ matches
3. behavioral checks: results, C++ exception wrapping, affine thread
   pinning (including returned affine values), pool execution,
   policy-driven surface drops, aclose
"""

import argparse
import ast
import asyncio
import importlib
import json
import pathlib
import sys


def test_parse(out: pathlib.Path):
    for py in sorted(out.glob("*.py")):
        ast.parse(py.read_text(), filename=str(py))


async def test_behavior():
    from fake_library_generated import (
        AsyncLocalStore,
        AsyncRemoteStore,
        AsyncStorePath,
        AsyncDerivation,
        AsyncDerivedPath,
        AsyncEvalState,
        AsyncValue,
    )
    from fake_library_generated._runtime import InternalError

    pkg_dir = pathlib.Path(importlib.import_module("fake_library_generated").__file__).parent
    manifest = json.loads((pkg_dir / "manifest.json").read_text())

    # Pool store: concurrent adds genuinely overlap.
    local = AsyncLocalStore()
    assert await local.get_uri() == "local"
    t0 = asyncio.get_running_loop().time()
    p1, p2 = await asyncio.gather(
        local.add_text_to_store("hello.txt", "world"),
        local.add_text_to_store("note.txt", "nix mock"),
    )
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 0.18, f"expected overlapped adds, took {elapsed:.2f}s"
    assert len({await p1.to_string(), await p2.to_string()}) == 2
    assert await local.is_valid_path(p1) is True

    # Opaque and built derived paths.
    req = AsyncDerivedPath(p1)
    out_opaque = await local.build_derivation(req)
    assert await out_opaque.to_string() == await p1.to_string()
    drv_req = AsyncDerivedPath(p2, "out")
    out_built = await local.build_derivation(drv_req)
    assert (await out_built.name_part()).endswith("-out")
    assert await local.is_valid_path(out_built) is True

    # Affine store: everything pinned to one dedicated thread, including
    # a slow add.
    remote = AsyncRemoteStore()
    assert await remote.get_uri() == "uds://daemon"
    await remote.is_valid_path(p1)
    await remote.add_text_to_store("slow.drv", "DrvFoobar")
    assert len(remote._runner.workers_seen) == 1, "affine calls must share one thread"

    # Returned affine value pins to the PRODUCER's thread.
    drv_path = await remote.add_text_to_store("mysite.drv", "DrvMine")
    drv = await remote.query_derivation(drv_path)
    d1 = await drv.describe()
    d2 = await drv.describe()
    assert "seen 1x" in d1 and "seen 2x" in d2
    assert await drv.queries() == 2
    assert drv._runner.workers_seen == remote._runner.workers_seen, (
        "derivation ops must run on the producer's thread"
    )

    # Returned pool values are free to use any thread.
    spool = await local.add_text_to_store("x", "y")
    assert isinstance(spool, AsyncStorePath)
    assert isinstance(drv, AsyncDerivation)
    assert isinstance(drv_req, AsyncDerivedPath)

    # Policy enforcement: the pool LocalStore may not expose an
    # affine-returning method, so the generator dropped it.
    assert "query_derivation" not in manifest["wrappers"]["LocalStore"]["methods"], (
        "affine-returning method must be dropped from pool wrapper"
    )
    assert not hasattr(local, "query_derivation")

    # C++ exceptions surface as InternalError with the cause attached.
    try:
        await remote.query_derivation(spool)
        raise AssertionError("expected InternalError for non-.drv path")
    except InternalError as e:
        d = e.to_dict()
        assert d["code"] == "internal" and d["cause_type"] == "ValueError"

    # Evaluation: EvalState is the affine SERVICE exemplar. Its values
    # attach to its thread, and forcing mutates them in place.
    state = AsyncEvalState("local")
    assert await state.get_store_uri() == "local"

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

    # Boehm GC proof, in two layers. First the counters bound straight
    # from gc.h prove the collector is ACTIVE and that this exact value
    # lives inside a GC-allocated block. A no-op integration could not
    # produce either fact.
    import fake_library
    stats = fake_library.gc_stats()
    assert stats["heap_size"] > 0 and stats["total_bytes"] > 0
    assert await v.is_gc_managed()
    assert await thunk.is_gc_managed()

    # Second layer: survival. Collection is a blocking global operation,
    # so it is dispatched off the loop thread - which also exercises
    # thread registration from a fresh pool thread.
    collections_before = stats["collections"]
    await asyncio.to_thread(fake_library.collect_garbage)
    assert fake_library.gc_stats()["collections"] >= collections_before + 2
    assert await v.string_value() == "hello nix"
    await asyncio.to_thread(fake_library.collect_garbage)
    # Forced state persists through collection...
    assert await thunk.type_name() == "int"
    assert await thunk.integer() == 42
    # ...and the arena keeps accepting new values afterwards.
    fresh = await state.parse_expr("7")
    assert await fresh.type_name() == "thunk"
    await state.force(fresh)
    assert await fresh.integer() == 7

    # Non-reachability collection, the Nix-faithful behavior: dropping
    # the wrapper frees its bridge cell, leaving nothing visible that
    # points at the value, so the collector reclaims it while the state
    # stays alive.
    kept = [await state.parse_expr(f'"{"p" * 200}-{i}"') for i in range(200)]
    await asyncio.to_thread(fake_library.collect_garbage)
    kept_used = fake_library.gc_stats()["used_bytes"]

    del kept
    await asyncio.to_thread(fake_library.collect_garbage)
    dropped_used = fake_library.gc_stats()["used_bytes"]
    # Counters are page-granular; any strict decrease proves values died
    # on non-reachability. Under the old arena design this could never
    # move while the state lives.
    assert dropped_used < kept_used, (
        f"dropped values must be reclaimed: used {kept_used} -> {dropped_used}"
    )

    assert v._runner.workers_seen == state._runner.workers_seen, (
        "value ops must run on the producer's thread"
    )

    # Affine serialization: two gathered evals take ~2x one eval.
    t0 = asyncio.get_running_loop().time()
    await asyncio.gather(state.eval_expr("1"), state.eval_expr("2"))
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed >= 0.075, f"evals must serialize, took {elapsed * 1000:.0f}ms"

    # Parse errors surface as InternalError with the C++ cause.
    try:
        await state.eval_expr("not an expression")
        raise AssertionError("expected parse error")
    except InternalError as e:
        d = e.to_dict()
        assert d["code"] == "internal" and d["cause_type"] == "ValueError"

    await v.aclose()
    await thunk.aclose()
    await state.aclose()

    # Construction failures are cached and re-raised identically.
    # Wrappers construct lazily, so the bad argument fails on first call.
    bad = AsyncRemoteStore("unexpected-arg")
    for _ in range(2):
        try:
            await bad.get_uri()
            raise AssertionError("expected construction failure")
        except InternalError as e:
            assert type(e.__cause__) is TypeError

    await drv.aclose()
    await remote.aclose()
    await local.aclose()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = pathlib.Path(args.out).resolve()

    test_parse(out)

    # Import the generated package from its parent dir, shadowing any
    # installed copy. Bindings (fake_library) come from PYTHONPATH.
    sys.path.insert(0, str(out.parent))
    importlib.invalidate_caches()
    asyncio.run(test_behavior())
    print("smoke test OK")


if __name__ == "__main__":
    main()
