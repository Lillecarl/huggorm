"""
Demo of generated async in-process wrappers.

    C++ fake-library (sync)
      -> Cython bindings (sync)
        -> spec.py services (IDL, subclasses of Cython types)
          -> Nix build-time AST codegen
            -> AsyncCat / AsyncSpider (awaitable, thread-policy aware)

Threading policy from the IDL:
- RemoteCat  (affine): constructed on + pinned to one dedicated thread
- RemoteSpider (pool): runs on a shared thread pool
"""

import asyncio

from fake_library_generated import AsyncCat, AsyncSpider


async def main():
    # Constructor args pass through; object is constructed lazily on its thread.
    cat = AsyncCat("Whiskers")
    spider = AsyncSpider("Shelob")

    print("=== sequential awaits ===")
    print(await cat.greet("you"))
    print(await cat.lives_remaining())
    print(await spider.crawl(2.5))
    print(await spider.bite("fly"))

    print("\n=== thread affinity (RemoteCat, affine) ===")
    await cat.greet("a")
    await cat.greet("b")
    await cat.lives_remaining()
    print(f"born on:   {cat._runner.born_thread_name}")
    print(f"last call: {cat._runner.last_worker_name}")
    print(f"workers seen: {sorted(cat._runner.workers_seen)}  <- must be exactly 1")

    print("\n=== thread pool (RemoteSpider, pool) ===")
    results = await asyncio.gather(
        spider.crawl(1.0),
        spider.crawl(2.0),
        spider.crawl(3.0),
        spider.bite("fly"),
    )
    print(f"results: {results}")
    print(f"workers seen: {sorted(spider._runner.workers_seen)}  <- multiple pool threads")

    print("\n=== concurrency works (asyncio) ===")
    # Interleave both services concurrently
    both = await asyncio.gather(cat.greet("x"), spider.crawl(9.9))
    print(f"{both}")

    print("\n=== typed error passes through untouched ===")
    try:
        await cat.greet("")
    except Exception as e:
        print(f"caught {type(e).__name__}: code={e.code!r} message={e.message!r}")
        print(f"serializable: {e.to_dict()}  <- this is what RPC will put on the wire")

    print("\n=== C++ exception surfaces as InternalError with cause chain ===")
    print(await cat.fetch("ball"))
    try:
        await cat.fetch("rock")
    except Exception as e:
        print(f"caught {type(e).__name__}: {e.to_dict()}")
        import traceback

        traceback.print_exception(e, limit=6)

    print("\n=== construction failure is cached, not retried ===")
    from fake_library_generated._runtime import InternalError

    # Cython Cat.__cinit__ requires a name; this factory always fails.
    bad = AsyncCat()
    seen = []
    for attempt in (1, 2, 3):
        try:
            await bad.lives_remaining()
        except InternalError as e:
            seen.append(e)
            print(f"attempt {attempt}: {type(e).__name__} <- {type(e.__cause__).__name__}: {e.__cause__}")
    print(f"fresh wrapper each call: {seen[0] is not seen[1]}; single cached root cause: {seen[0].__cause__ is seen[2].__cause__}")

    await asyncio.gather(cat.aclose(), spider.aclose())
    print("closed cleanly")


if __name__ == "__main__":
    asyncio.run(main())
