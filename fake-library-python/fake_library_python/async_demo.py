"""
Demo of generated async in-process wrappers.

    C++ fake-library (sync)
      -> Cython bindings (sync)
        -> spec.py services (IDL, subclasses of Cython types)
          -> Nix build-time AST codegen
            -> AsyncRemoteCat / AsyncRemoteSpider (awaitable, thread-policy aware)

Threading policy from the IDL:
- RemoteCat  (affine): constructed on + pinned to one dedicated thread
- RemoteSpider (pool): runs on a shared thread pool
"""

import asyncio

from fake_library_generated import AsyncRemoteCat, AsyncRemoteSpider


async def main():
    # Constructor args pass through; object is constructed lazily on its thread.
    cat = AsyncRemoteCat("Whiskers")
    spider = AsyncRemoteSpider("Shelob")

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

    await asyncio.gather(cat.aclose(), spider.aclose())
    print("closed cleanly")


if __name__ == "__main__":
    asyncio.run(main())
